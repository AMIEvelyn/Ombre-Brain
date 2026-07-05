# 时间线 / 事实档案层 —— 设计方案（memzep 架构本地化）

写给：一澜、林湛
状态：草案，等确认后再分阶段实现，每一步都可回退

## 一句话说清楚这是什么

不接入 mem0、Zep 本尊（避免重新提取 1700 万字的费用，也避免"两套系统谁说了算"的冲突）。而是把它们公开的**设计思路**——双时态时间戳、事实软失效（新事实不覆盖旧事实，而是给旧事实标注"到什么时候为止是真的"）、因果/时序关系图、按时间意图重排——用 OB 自己的存储方式实现一遍。

对应林湛说的三层：

| 层 | 是什么 | 现状 |
| --- | --- | --- |
| 心脏 | OB 现有的情绪记忆池，负责亲密感、自然遗忘与浮现 | 已有，不动 |
| 房契 | 原始对话/导入原文，一字不改、不参与遗忘 | 已有（`raw_events`），本方案不涉及 |
| 骨架 | 稳定事实 + 时间线 + 因果关系，**不参与遗忘曲线，精确可查** | **本方案新建** |

骨架这一层会做成 OB 后端里一个独立的存储（不是记忆桶 markdown 文件里加字段），但**界面上就是 Dashboard 里新加一个标签页**，对你们俩来说是"OB 多了一块"，不是另一个要单独登录、单独维护的东西。

## 为什么现有的"遗忘曲线"设计不适合直接拿来做这件事

OB 的衰减曲线（艾宾浩斯式指数衰减）是故意设计成"不那么精确"的——权重高的记忆容易被想起，权重低的会沉底，这是为了模拟"人的记忆是模糊的、被情绪筛选过的"。

但"林湛现在多高体重多少""他喜欢的东西现在是什么""3月15号那天发生了什么"——这类问题需要的是**精确答案**，不能因为这条记忆很久没被提起就变得难以查到，也不能因为新旧两条互相矛盾的事实同时躺在池子里而给出模糊或过时的答案。这就是骨架层要单独存在、不受衰减影响的原因。

## 新增的存储：`facts.sqlite`

跟记忆桶（markdown 文件）、原文保险箱（`raw_events.sqlite`）平行的第三个独立文件，不参与衰减、不参与情绪打分。

```sql
CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  subject_key TEXT NOT NULL,      -- 谁：user / lin_zhan / 其他人
  predicate_key TEXT NOT NULL,    -- 哪类事实：lives_in / height / weight / preference_coffee ...
  object_text TEXT NOT NULL,      -- 具体内容："165cm" "杭州" "拿铁"
  state_key TEXT NOT NULL,        -- = subject_key + predicate_key，同一件事的演变链
  valid_at TEXT,                  -- 这件事从什么时候开始为真（事件时间）
  invalid_at TEXT,                -- 到什么时候不再为真（NULL = 现在仍然有效）
  confidence REAL DEFAULT 0.9,
  evidence_bucket_id TEXT,        -- 证据来自哪个记忆桶（沿用现有 profile_fact 的证据机制）
  created_at TEXT DEFAULT CURRENT_TIMESTAMP  -- 系统写入时间，跟 valid_at 分开
);
CREATE INDEX idx_facts_state ON facts(state_key);
CREATE INDEX idx_facts_valid ON facts(valid_at, invalid_at);

CREATE TABLE timeline_edges (
  id INTEGER PRIMARY KEY,
  from_fact_id INTEGER,           -- 也可以指向普通记忆桶 id，二选一
  from_bucket_id TEXT,
  to_fact_id INTEGER,
  to_bucket_id TEXT,
  relation_type TEXT NOT NULL,    -- causes / leads_to / precedes（复用现有 memory_edges.py 的枚举）
  confidence REAL DEFAULT 0.5,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

"当前事实"查询：`WHERE state_key = ? AND invalid_at IS NULL`
"某天/某段时间为真的事实"查询：`WHERE valid_at <= ? AND (invalid_at IS NULL OR invalid_at > ?)`

## 怎么免费从现有数据里搭出这一层（不用重新调 AI）

1. **`profile_fact` 迁移**：现有的 profile_fact 桶已经带 `subject`/`predicate`/`object`/`evidence` 元数据（写代码时就是这么设计的），可以用纯本地脚本（不调 AI）把它们按 `subject_key + predicate_key` 分组、按证据桶的事件日期排序，自动生成 `state_key` 演变链——同一类事实里，日期早的自动标注 `invalid_at` = 下一条的 `valid_at`，最新的一条 `invalid_at` 留空。跟这次补日期的思路完全一样：先出一份"打算怎么分组"的报告给你确认，再正式写入。
2. **因果/时序边迁移**：`memory_edges.py` 里已经有的 `causes`/`precedes` 这些关系，直接原样搬进 `timeline_edges`，不用重新生成。
3. **往后新增**：`profile_fact` 工具写入新事实时，自动检查同一 `state_key` 有没有"当前有效"的旧记录，有就自动软失效旧的（这是唯一需要动"正在运行的代码逻辑"的地方，其余都是一次性迁移脚本）。

## 查询怎么感知"现在 vs 以前"

在 `breath()` 或新的一个查询入口里，简单识别几类关键词（"现在/目前/最近" vs "以前/曾经/那时候/上次" vs "以后/将来"），决定去 `facts` 表查"当前有效"还是"某个历史时间点"，不用为此接入什么智能语义模型，规则匹配就够用，这也是 mem0/Zep 报告里说的"时间只做加分/筛选依据，语义相关性优先"的做法。

## Dashboard 新标签页大概什么样

- **时间线视图**：按日期从早到晚一条条排（复用今天已经修好的事件日期数据），点开能看到"这天发生了什么、这句话原话在哪个记忆桶"
- **档案卡片视图**：每类稳定事实一张卡片（身高、体重、口味……），显示当前值 + "上次更新于"，点进去能翻到历史上这条事实变化的记录

## 分几步做（每步都先出报告、人工确认、再正式写入，不会一步到位改数据）

1. **建 `facts.sqlite` 表结构**（空表，纯新增，无风险）
2. **迁移现有 `profile_fact` → `facts` 表**（本地脚本，先出分组报告确认，再写入）
3. **迁移现有 `memory_edges` 因果边 → `timeline_edges`**（本地脚本，无需确认，纯复制）
4. **改 `profile_fact` 工具，支持新事实自动软失效旧事实**
5. **加查询时间意图识别**
6. **Dashboard 新标签页**（时间线 + 档案卡片两个视图）

每一步之间会给你看结果、确认没问题再往下走，跟今天补日期的节奏一样。
