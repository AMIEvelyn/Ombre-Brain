# 未来线 / 成就册 —— 设计方案（plans/achievements 模块）

写给：一澜、林湛
状态：设计已讨论确认，**尚未实现**，先存档，等排到日程再动手（不占用当前骨架层的工期）

**完成计划弹出的成就卡，跟骨架层的事实卡是同一套地基（标题+内容+附件+时间线）**，当前权威设计存档在 `docs/facts-model-v2-collection-redesign.md`（2026-07-16 更新：骨架层已经历一次模型重构，卡片改成收藏/歌单式的卡+文件夹多对多，之前指向 `facts-card-and-batch-import-plan.md` 的旧引用已不是当前设计），实现时两边应该共用同一套 UI 组件和后端字段，不要各做一套。

## 一句话说清楚这是什么

给 OB 加第四层，跟"心脏（情绪记忆池）/ 房契（原文保险箱）/ 骨架（facts，管过去和现在）"并列——**未来线**：管"还没发生、但想让它发生"的事，用游戏成就式的体验（解锁/进行中/已完成/封存/旧档案），而不是冷冰冰的待办表格。完成的计划会自动生成一条骨架层的 `historical_event` 事实，让未来线和骨架接上；需要写感想时提示真人自己写，不自动生成。

## 为什么不是塞进 facts 表

`facts` 的核心是 valid_at/invalid_at 软失效状态机，是为"事实会不会被新的取代"设计的。计划的状态流转（`planned → in_progress → done/paused/cancelled`）是完全不同的状态机，硬塞会很别扭。**架构模式可以复用**（独立 SQLite 小仓库、不参与遗忘曲线、带证据链接），但表结构和状态机是独立的一套。

## 表结构：`plans.sqlite`

```sql
CREATE TABLE plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_key TEXT NOT NULL,               -- relationship（多数情况）/ yi_lan / lin_zhan
  title TEXT NOT NULL,                     -- 计划标题
  description TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'other',  -- travel/ritual/ring/money/home/daily/tech/other
  status TEXT NOT NULL DEFAULT 'planned',  -- planned/in_progress/done/paused/cancelled
  priority INTEGER NOT NULL DEFAULT 5,     -- 1-10，纯排序用
  target_date TEXT,                        -- 目标时间，可空
  created_at TEXT NOT NULL,
  done_at TEXT,                            -- 完成时间，可空
  evidence_bucket_id TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  reflection_lin_zhan TEXT NOT NULL DEFAULT '',  -- 完成时提示填写，不自动生成
  reflection_yi_lan TEXT NOT NULL DEFAULT '',    -- 同上
  linked_fact_id INTEGER                    -- 完成后在 facts.sqlite 里自动生成的 historical_event 记录 id
);
```

`category` 不需要像 predicate_mode 那样建登记表——它只影响 UI 分类展示，不影响任何行为逻辑，普通字符串字段就够，不要多套一层。

## 状态机（技术状态 ↔ 游戏化呈现）

| 技术状态 | UI 呈现 |
| --- | --- |
| planned | 还没解锁的小任务 |
| in_progress | 正在推进 |
| done | 成就点亮 |
| paused | 暂时封存 |
| cancelled | 放进旧计划档案（不删除，留档） |

## 完成时的流程

1. 用户/林湛把某条计划标记完成（`status = done`，写 `done_at`）
2. **弹出小表单，提示双方各自写一句感想**（`reflection_lin_zhan` / `reflection_yi_lan`）——不自动生成，写不写随意，这是为了不让系统代笔冲淡真实感
3. 自动在 `facts.sqlite` 里生成一条 `subject_key=relationship, predicate_key=relationship_event, mode=historical_event` 的事实，记录"这个计划完成了"，并把新事实的 id 存进 `linked_fact_id`，让未来线和骨架层连起来
4. 完成后前端展示一张"成就卡片"：完成时间、当时说的话（notes/description）、双方感想、关联记忆桶链接

## Dashboard：单独一个"计划"标签页，不跟骨架混在一起

- 默认显示未完成的（planned/in_progress/paused）
- 已完成的折叠进"完成纪念册"
- 按 category 分区，参考例子：
  - 旅行类：青海湖、香港小日子……
  - 仪式类：黄大仙还愿、戒指升级、纪念日
  - 家园类：买房、攒钱、给林湛更稳定的家
  - 日常类：一起煮火锅、看电视、遛狗
  - 技术类：OB 心脏、骨架层、原文房间、梦境、专属前端（工程路线本身也可以当成就来记！）
- 视觉上要做成"成就列表"的感觉（分类、徽章、进度、纪念册），不能做成后台管理表格

## MCP 工具（以后再加，命名从可爱出发）

- `plan_lookup`：查"我们还有哪些计划""黄大仙还愿做了吗""青海湖旅行还在计划里吗"
- `plan_update` / `achievement_unlock`：标记状态变化、完成

## 跟 memzep 骨架层的关系

两者是平级的独立模块，共享同一种"独立 SQLite + 不参与衰减 + 证据链接"的架构精神，但不共用表、不共用状态机。完成计划 → 骨架层 historical_event 是唯一的数据流向（未来线单向流入骨架，骨架不会反过来影响未来线）。

## 现状

纯设计存档，**没有开始写代码**。排期上建议在 memzep 骨架层的"自动判断逻辑""手动写入 UI"这些收尾之后再启动，避免同时铺太多摊子。
