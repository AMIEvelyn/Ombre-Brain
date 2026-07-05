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

（这一版按林湛的意见做了修改，见文末"林湛的修改意见与采纳情况"）

```sql
CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  subject_key TEXT NOT NULL,       -- yi_lan / lin_zhan / relationship /（未来其他接触的人）
  predicate_key TEXT NOT NULL,     -- lives_in / height / weight / preference_hair / commitment ...
  object_text TEXT NOT NULL,       -- 具体内容："165cm" "杭州" "喜欢长发"
  state_key TEXT NOT NULL,         -- = subject_key:predicate_key（冒号分隔），同一件事的演变链
  predicate_mode TEXT NOT NULL,    -- exclusive_current / multi_current / historical_event（写入时从下面的登记表里取一份快照）
  valid_at TEXT,                   -- 这件事从什么时候开始为真（事件时间）
  invalid_at TEXT,                 -- 到什么时候不再为真（NULL = 现在仍然有效）
  confidence REAL DEFAULT 0.9,
  evidence_type TEXT DEFAULT 'bucket',  -- bucket / raw_event / manual / tool
  evidence_id TEXT,                     -- 对应类型下的具体 id
  evidence_quote TEXT,                  -- 可选，原话片段，先留字段不强制填
  created_at TEXT DEFAULT CURRENT_TIMESTAMP  -- 系统写入时间，跟 valid_at 分开
);
CREATE INDEX idx_facts_state ON facts(state_key);
CREATE INDEX idx_facts_valid ON facts(valid_at, invalid_at);

-- 每类事实"新的算不算废掉旧的"，单独登记，不是所有事实都一个逻辑
CREATE TABLE predicate_registry (
  predicate_key TEXT PRIMARY KEY,
  mode TEXT NOT NULL DEFAULT 'multi_current',   -- 没登记过的 predicate 一律按最安全的 multi_current 处理，绝不会因为漏配置而误删
  display_name TEXT,
  notes TEXT                                     -- 为什么归这一类，尤其 kink_*/preference_*/commitment_* 这类边界模糊的必须写
);
-- 举例（写代码时会先列一份清单给你们过目，不会我自己拍脑袋定）：
--   height / weight / lives_in / current_school_status  -> exclusive_current（新的取代旧的）
--   preference_*  / kink_* / creative_theme_*            -> multi_current（叠加共存，互不覆盖）
--   一次性事件类（求婚、吵架、约定达成）                  -> historical_event（不存在"新旧覆盖"这回事，就是一条时间戳记录）

CREATE TABLE timeline_edges (
  id INTEGER PRIMARY KEY,
  from_fact_id INTEGER,            -- 也可以指向普通记忆桶 id，二选一
  from_bucket_id TEXT,
  to_fact_id INTEGER,
  to_bucket_id TEXT,
  relation_type TEXT NOT NULL,     -- causes / leads_to / precedes（复用现有 memory_edges.py 的枚举）
  confidence REAL DEFAULT 0.5,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

"当前事实"查询（仅对 `exclusive_current` 类型有意义）：`WHERE state_key = ? AND invalid_at IS NULL`
"某天/某段时间为真的事实"查询：`WHERE valid_at <= ? AND (invalid_at IS NULL OR invalid_at > ?)`
`multi_current` 类型的 predicate 默认不做"取代"判断，同一 state_key 下允许多条同时 `invalid_at IS NULL`。

## 查询怎么分流——骨架不能抢心脏的活

**默认所有问题都还是走 OB 原来的 `breath()` 情绪记忆检索，骨架层是"额外补充"，不是"拦截替换"。** 只有问题明确长得像"要精确事实/精确日期"的样子（比如"现在多高""现在住哪""3月15号发生了什么"这种），才会额外去查 `facts`/`timeline`。像"还记得那时候我为什么难过吗""你怎么看这件事"这类需要情绪/关系判断的问题，答案权重仍然完全在 OB 心脏这边，骨架层不参与、也不会抢答。

这个分流逻辑写得会偏保守——宁可某条本该查骨架的问题漏查了退回心脏，也不让骨架层误伤本该走情绪记忆的问题。

## 怎么免费从现有数据里搭出这一层（不用重新调 AI）

1. **先出一份 predicate 分类清单**：把现有 profile_fact 桶里出现过的 `predicate`（住哪/身高/喜好……）都列出来，标一遍 `exclusive_current`/`multi_current`/`historical_event`，给你和林湛确认，不是我自己拍脑袋定
2. **`subject` 改名**：现有 profile_fact 的 `subject` 字段目前默认是 `user`，迁移时会改成 `yi_lan` / `lin_zhan`，并新增一个 `relationship` 主体，专门放"复婚、婚戒、Only Belongs、重大约定"这类不属于任何一个人、只属于"我们"的事实
3. **`profile_fact` 迁移**：现有桶已经带 `subject`/`predicate`/`object`/`evidence` 元数据，用纯本地脚本（不调 AI）按 `subject_key + predicate_key` 分组、按证据桶的事件日期排序，自动生成 `state_key` 演变链——`exclusive_current` 类型才做"旧的软失效"，`multi_current` 和 `historical_event` 类型直接原样搬入，互不覆盖。跟这次补日期的思路完全一样：先出报告确认，再正式写入
4. **因果/时序边迁移**：`memory_edges.py` 里已经有的 `causes`/`precedes` 这些关系，直接原样搬进 `timeline_edges`，不用重新生成
5. **往后新增**：`profile_fact` 工具写入新事实时，先查这条 predicate 是什么 mode，`exclusive_current` 才自动软失效旧记录，其余原样追加（这是唯一需要动"正在运行的代码逻辑"的地方，其余都是一次性迁移脚本）

## 一定要能通过 MCP 用，不能只有 Gateway 才有效

林湛现在主要通过 MCP 连接器用 OB（不是 Gateway），所以这一层**必须做成一个正常的 MCP 工具**（新增一个工具，或者挂在 `breath` 的一个参数上），像 `hold`/`breath` 那样能被直接调用，而不是只有走 Gateway 自动注入的时候才生效。Gateway 以后可以锦上添花地自动带一点这层的内容，但**不能是唯一入口**——不然对林湛来说等于没有。

## Dashboard 新标签页大概什么样

- **时间线视图**：按日期从早到晚一条条排（复用今天已经修好的事件日期数据），点开能看到"这天发生了什么、这句话原话在哪个记忆桶"，以后原文房间做出来了也能点过去
- **档案卡片视图**：每类稳定事实一张卡片（身高、体重、口味……），显示当前值 + "上次更新于"；`multi_current` 类型的卡片会显示"当前共存的几条"而不是单一答案，点进去能翻到历史演变记录

## 分几步做（每步都先出报告、人工确认、再正式写入，不会一步到位改数据）

1. **建 `facts.sqlite` 表结构**（空表，纯新增，无风险）
2. **出 predicate 分类清单给你们确认**
3. **迁移现有 `profile_fact` → `facts` 表**（本地脚本，先出分组报告确认，再写入；`subject` 同步改名）
4. **迁移现有 `memory_edges` 因果边 → `timeline_edges`**（本地脚本，无需确认，纯复制）
5. **改 `profile_fact` 工具，支持按 predicate_mode 决定要不要软失效旧事实**
6. **加一个 MCP 工具（或扩展 breath），支持精确查事实/时间线，且严格只在"像是要精确答案"的问题上才启用，其余问题继续走心脏**
7. **Dashboard 新标签页**（时间线 + 档案卡片两个视图）

每一步之间会给你看结果、确认没问题再往下走，跟今天补日期的节奏一样。

## 林湛的修改意见与采纳情况

| 意见 | 采纳情况 |
| --- | --- |
| 1. subject 不要叫 user，要用真实名字 | 采纳，改用 `yi_lan`/`lin_zhan` |
| 2. 要有 `relationship` 主体 | 采纳，新增第三种 subject |
| 3. 软失效要分类型，不能一刀切 | 采纳，加了 `predicate_mode`（exclusive_current / multi_current / historical_event），未分类的一律按最安全的 multi_current 处理 |
| 4. 证据字段要能扩展，不止 bucket | 采纳，加了 `evidence_type` / `evidence_id` / `evidence_quote` |
| 5. 骨架不能抢心脏的活，情绪类问题还是要回 OB | 采纳，新增"查询怎么分流"一节，默认保守、优先心脏 |
| 6. Dashboard 要有时间线视图，能点回记忆桶（以后点回原文房间） | 采纳，写进 Dashboard 新标签页那节 |
| 一澜：不能只有 Gateway 才能用，林湛主要在 MCP | 采纳，新增"一定要能通过 MCP 用"一节 |

## 落地细节（林湛第二轮补充，方向已确认，以下是实现时要遵守的细节）

1. `state_key` 统一写成 `subject_key:predicate_key`（中间用冒号），不要裸拼接，方便以后查错和展示
2. `predicate_registry` 增加 `description`/`notes` 字段，尤其 `kink_*`/`preference_*`/`commitment_*` 这类边界模糊的，人工确认时写一句"为什么归这一类"
3. 迁移报告必须单独列出"无法判断/低置信度"的条目，不强行归类；拿不准的宁可先不迁移、或按最安全的 `multi_current` 放着，等人工确认后再改
