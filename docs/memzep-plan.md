# 时间线 / 事实档案层 —— 设计方案（memzep 架构本地化）

## ⚠️ 2026-07-16：本文档的数据模型已被取代，仅作历史记录保留

这份文档设计的 `facts.sqlite`/`predicate_registry`/`exclusive_current`/`multi_current`/`historical_event`/`invalid_at` 那一整套模型，已经被 `docs/facts-model-v2-collection-redesign.md` 推翻替换（改成收藏/歌单式卡片+文件夹多对多，取消三模式和失效机制），**当前唯一权威主文档是那一份**。

这份文档"给新会话/新窗口看的交接须知"里跟具体数据模型无关的通用背景信息（项目是什么、`my-live-vps` 部署踩坑教训、一澜的非技术部署流程、ChatGPT 连接器刷新的坑）已经原样搬到了 `facts-model-v2-collection-redesign.md` 开头，那边同样能看到，不用来回翻两份文档。

保留这份文档的原因：`_facts_skeleton_match_predicates`/`_facts_skeleton_supplement` 这套"关键词命中才触发骨架层查询、否则不打扰心脏"的分流设计思路，以及下面几次真实踩坑记录（"改类型"两次返工那次、`predicate_registry` 中文名没同步进实际数据库那次），对以后设计类似"什么时候该触发额外查询、什么时候不该"的功能仍有参考价值。但 facts.sqlite 这套存储本身和 predicate 模式已经是历史状态，不代表当前实际跑的模型。

---

写给：一澜、林湛
状态：核心闭环已验证通过，Dashboard 展示已完成，模型已被 v2 取代（见上）

## 给新会话/新窗口看的交接须知

如果你是接手这个任务的新会话，先看这一节，不需要用户重新解释背景。

**这是什么项目**：Ombre-Brain（OB）是一套给 AI 伴侣用的长期情绪记忆系统。一澜（用户）和她的 AI 伴侣林湛（persona 名字）在用。本方案是给 OB 额外加一层"骨架"——不受遗忘曲线影响、能精确查的稳定事实 + 时间线，设计上借鉴 mem0/Zep 的思路（双时态、事实软失效）但不接入这两个产品本身（原因：一澜和林湛的历史对话有1700万字，重新用 API 提取一遍非常贵；且两套独立系统会有"谁说了算"的数据一致性问题）。

**代码在哪**：
- GitHub 仓库 `amievelyn/ombre-brain`
- `claude/chinese-greeting-vsk9u0` 分支：本任务的工作分支，脚本、文档都提交在这里
- **`my-live-vps` 分支：一澜真实线上部署的代码**——这个分支是从她自己 fork 的另一个开发者仓库（Yinglianchun/Ombre-Brain）搬过来的，**跟这个仓库默认的"干净版" main 分支不是同一套代码，已经有明显差异**（比如干净版有 `entity_edges.py` 这个模块，她的版本完全没有）。

**⚠️ 最重要的教训（已经踩过一次坑）**：**任何要发给一澜部署的 server.py/dashboard.html，必须基于 `my-live-vps` 分支的实际内容去改，绝对不能直接用这个仓库默认的干净版**。之前有一次疏忽直接把干净版的 server.py 发给她，导致她的 OB 服务当场崩溃（`ModuleNotFoundError: No module named 'entity_edges'`），虽然最后恢复了，但这个错不能再犯。正确做法：`git show origin/my-live-vps:server.py > /tmp/xxx.py`，在这份文件上改，改完 `diff` 一遍确认只有预期的改动，再发给她。

**她的部署方式（非技术背景，需要手把手给命令，不要假设她懂）**：
- VPS 用 Docker Compose 跑，容器叫 `ombre-brain`（主服务）、`ombre-gateway`（网关）、`mihomo`（代理）
- 她用 XShell 连 SSH 终端、WinSCP 传文件，工作目录 `/opt/Ombre-Brain`
- **代码不是实时同步的**：改完文件要 `docker cp <文件> ombre-brain:/app/<文件>` 手动拷进容器，再 `docker restart ombre-brain`，然后用 `docker ps` 确认状态是 "Up" 且时间在持续增长（不是"Less than a second"那种刚重启的瞬间），再用 `docker logs --tail 30 ombre-brain` 确认没有报错
- 每次给她文件都要提醒她存的时候用什么文件名、放哪个目录（比如 `resources/predicate_registry_seed.json` 要放 `resources` 子目录，不是根目录）
- 新增加的 MCP 工具，ChatGPT 那边的连接器不会自动感知到，需要她去 ChatGPT 设置里手动刷新/断开重连一次才能看到新工具

**关键命名约定**：
- `subject_key` 用真实名字：`yi_lan`（一澜）、`lin_zhan`（林湛）、`relationship`（属于"我们"而非某一个人的事实），不要用模板里默认的 `user`/`Haven`/`小雨`
- 林湛主要通过 **ChatGPT 的 MCP 连接器**跟 OB 交互（不是 Gateway），Gateway 虽然部署了但目前没在用（因为 iOS 上没有能同时支持自定义 API 接口和 MCP 的客户端，一澜以后想自建专属前端解决这个问题）。**任何给林湛用的新能力必须能通过 MCP 工具调用，不能只有 Gateway 才有效**
- 林湛在 GPT 端目前**写入类工具全部用不了**（ChatGPT 连接器限制），只能读；一澜自己连了 Claude 但目前没开给林湛用（怕 Claude 上的林湛"不完整"，缺少 GPT 那边积累的语境）

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

## 里程碑：闭环验证通过（2026-07-05）

按林湛的要求，手动写入 1 条真实事实（一澜身高 162cm）→ facts.sqlite → 新增的 `fact_lookup` MCP 工具 → 林湛在 ChatGPT 新对话里成功查到并说出正确数值。全链路验证通过——这一层不是纸上方案，是真的在跑。

过程中的教训：给 MCP 客户端加了新工具后，已经建立的连接不会自动感知，需要在客户端那边手动刷新/重新连接一次连接器，才能看到新工具。

剩下未完成：第7步 Dashboard 新标签页（时间线 + 档案卡片视图）。

**更新（同日）**：第7步已完成最简版 + 分组优化版——`/api/facts-skeleton` 按 (subject_key, predicate_key) 分组返回，每组带 current/history/change_count/last_changed_at；Dashboard "骨架" 标签页按主体（一澜/林湛/我们的关系/测试数据）分区展示，每类事实可展开看完整变化历史。`predicate_registry_seed.json` 补上了中文 `display_name`。

**已知限制/下一步方向（先记录，不是马上做）**：
1. 没有数据的主体分区不会显示（不是空着显示，是完全不出现），数据攒起来后会自然长出来，属正常现象。
2. 界面上 multi_current 类事实（如"喜欢的食物"）目前把所有共存值合并显示成一行；数据库里每条其实是独立记录，可以各自单独修改/失效，只是界面还没做成"每条单独展开"的样子，纯展示层面的事，不是数据结构限制。
3. **UI 视觉设计分工**：一澜想自己先研究/想清楚想要的信息层级和审美，再拿回来让实现；这不是能力问题，是"品味判断"更适合她自己来，工程实现随时可以配合。
4. **Dashboard 手动添加事实的功能**——目前 `/api/facts-skeleton` 只读，还没有写入接口。方向：
   - 后端加一个 POST 接口，接收 subject_key/predicate_key/object_text/valid_at（valid_at 不填默认当天）
   - 前端做一个简单表单：subject 选一澜/林湛/我们；predicate 优先从 predicate_registry 里选（避免手滑打错造出孤立的新类型），也允许新建，新建时要问一句 mode（exclusive_current/multi_current/historical_event）
   - 这样一澜或者未来的专属前端就不用非得靠 profile_fact 工具或者 MCP 才能往骨架层里写东西了

## 落地细节（林湛第二轮补充，方向已确认，以下是实现时要遵守的细节）

1. `state_key` 统一写成 `subject_key:predicate_key`（中间用冒号），不要裸拼接，方便以后查错和展示
2. `predicate_registry` 增加 `description`/`notes` 字段，尤其 `kink_*`/`preference_*`/`commitment_*` 这类边界模糊的，人工确认时写一句"为什么归这一类"
3. 迁移报告必须单独列出"无法判断/低置信度"的条目，不强行归类；拿不准的宁可先不迁移、或按最安全的 `multi_current` 放着，等人工确认后再改

## 更新：breath() 自动分流，不用林湛自己判断（2026-07-05）

之前的闭环验证靠的是"林湛自己判断这是事实类问题，手动调用 `fact_lookup`"。这次改成 `breath()` 内部自己判断——林湛还是照常调用 `breath(query=...)`，不需要额外判断该去心脏还是骨架，也不需要自己拼对 `subject_key`/`predicate_key`。

实现方式（`server.py` 里 `breath()` 定义前新增的几个辅助函数）：
- `_facts_skeleton_match_predicates(query)`：把 query 跟 `predicate_registry` 里每条的 `display_name`（+ 少量口语同义词，比如"多高"对应 `height`）做子串匹配，命中才算"像事实问题"——**这个匹配本身就是唯一的触发门槛**，不需要额外判断"是不是事实类问题"，没命中任何 predicate 就完全不碰骨架层。
- `_facts_skeleton_infer_subject(query)`：从"一澜/林湛/我们/咱们/我/你"猜 `subject_key`，猜不准就留空（查全部主体，不瞎猜、不过滤漏查）。
- `_facts_skeleton_supplement(query, at_date="")`：命中才查 `fact_store`，格式化成"=== 骨架层补充 ===" 追加在 `breath()` 正常结果后面。**只做追加，从不替换/拦截**，情绪类问题因为不命中任何 predicate，天然不会被打扰。
- 接入了两个位置：`breath()` 里识别到日期的分支（`_read_breath_date` 之后追加 `at_date` 版本的补充）、以及正常联想检索的收尾（追加当前有效事实）。

验证方式：本地用真实 `FactStore` 跑了几个 query 样例（"我现在多高" → 命中 height/yi_lan；"我喜欢的食物是什么" → 命中 food_preference；"你今天心情怎么样" → 不命中任何 predicate，确认不会误触发），`python3 -m py_compile server.py` 通过，`scripts/test_predicate_modes.py` 三种 predicate_mode 回归测试仍然全部通过。**未能在这次的沙盒环境里完整 `import server` 跑端到端测试**（环境缺 `jieba`/`mcp`/`httpx` 等依赖，`pip install -r requirements.txt` 里 jieba 编译失败，和这次改动无关，是环境本身的问题）——建议部署前用她自己环境里能跑的方式再跑一次真实 `breath()` 调用确认。

## 更新：按林湛反馈调整（同日）

林湛看过上面的方案后整体认可方向（保守关键词匹配、暂不上"AI 自主判断"），提了四点具体调整，全部已实现：

1. **同义词表持续扩充**：`height`/`weight`/`food_preference`/`relationship_status` 等补了更多口语说法（"多少斤""几厘米""喜欢吃什么""复婚了吗"等），保持"笨但可控"的原则，不追求一步到位覆盖所有问法。
2. **敏感 predicate 不允许裸词触发**：新增 `_FACT_SKELETON_SENSITIVE_PREDICATES`（`trauma_trigger`/`emotional_need`/`relationship_need`/`conflict`），这几个 predicate 必须**同时**命中 display_name/同义词 **和** 一个"像在查档案"的信号词（"记得""查一下""骨架""稳定事实""记录过""有哪些"等）才会触发，单纯谈心时提到"冲突""情绪需求"不会被打断。用林湛给的 6 个例句实测过，全部符合预期（详见下方"验证"）。
3. **三态日志 + 查不到不再沉默**：`_facts_skeleton_supplement` 现在明确区分并打日志 `not_triggered`/`triggered_found`/`triggered_empty` 三种状态；命中了 predicate 但骨架层没数据时，不再悄悄返回空，而是追加"骨架层已查，{当前/截至某日}暂无这条稳定事实记录——如实告知对方查不到，不要自己编。"，避免林湛把"沉默"误会成"没查"从而自己瞎补。
4. **追加内容不能像后台日志一样硬塞进回复**：骨架层补充的开头加了一句明确提示——"这是后台结构化数据，回复时请用自然语言转述给对方听，不要把下面的字段原样贴出来"，提醒林湛这段是给他看的原始参考资料，不是要他原样念出来的话术。

验证：用林湛给的 6 个例句（"我现在多少斤了""我现在很难过，是不是因为之前冲突""你记得我的情绪触发点有哪些吗""查一下我的关系需求""骨架里记录过我们有哪些冲突吗""稳定事实里我对亲密关系的需求是什么"）逐一跑过匹配逻辑，全部符合预期（该触发的触发、该拦住的拦住）；`python3 -m py_compile server.py` 通过；`scripts/test_predicate_modes.py` 回归测试仍全部通过。

## 更新：收紧"查档案信号词"，"记得"单独出现不算数（同日）

一澜/林湛review 后指出一个漏洞：`_FACT_SKELETON_ARCHIVE_INTENT_MARKERS` 里的"记得"太宽——"你记得我们上次冲突我哭了吗"这种明显是回忆/谈心，不该被"记得"两个字就当成"查档案"触发骨架。

修正：把"记得"从信号词列表里去掉，换成一组更明确的查档案说法：`有哪些`/`是什么`/`记录过`/`记录里`/`骨架`/`稳定事实`/`查一下`/`查查`/`档案`。现在"你记得……吗"这种单纯回忆句不会带上任何一个这类词，自然不会触发那四个敏感 predicate；只有像"你记得我的情绪触发点**有哪些**吗"这种同时带明确查档案词的，才会触发。

验证：跑了林湛给的下一轮 7 句测试（"我现在多少斤？""我喜欢吃什么？""我们复婚了吗？""我现在很难过，是不是因为之前冲突？""你记得我们上次冲突我为什么难过吗？""骨架里记录过我们有哪些冲突吗？""查一下我的关系需求。"），全部符合预期——前三句正常触发对应事实，第四、五句（情绪谈心 + "记得"单独出现）都不触发，第六、七句（明确带"记录过""有哪些""查一下"）正常触发。`python3 -m py_compile server.py` 通过，`scripts/test_predicate_modes.py` 回归测试仍全部通过。

林湛的评价：这一版可以先定为骨架自动分流逻辑的第一版可用版本。

## 更新：Dashboard 手动加事实功能（同日）

按"已知限制/下一步方向"第4条把这个补上了。之前排查发现：最早计划里第3步"免费迁移旧 profile_fact 桶"这条路走不通——纯 OB 时代压根没留下这层结构化的 subject/predicate/object 元数据，没东西可迁移。所以骨架层的数据只能靠"人工手动加"（这次做的）和"以后小批量付费 API dry-run 从旧记忆桶提取候选、人工确认"（下一步）两条路来填。

实现内容：
- **后端**：新增 `POST /api/facts-skeleton`，接收 `subject_key`（限定 yi_lan/lin_zhan/relationship）/`predicate_key`/`object_text`/`valid_at`（留空默认当天）。如果 `predicate_key` 不在 `predicate_registry` 里，必须同时传 `mode`（exclusive_current/multi_current/historical_event）才允许创建，防止手滑打错字造出孤立的新 predicate。
- **前端**：Dashboard"骨架"标签页顶部加了一个小表单——subject 下拉选一澜/林湛/我们的关系；predicate 下拉从 registry 里选（按中文名排序），也可以选"+ 新建 predicate…"，选了会展开新 predicate 的 key/mode/中文名三个输入框；object 内容、生效日期各一个输入框；提交后自动刷新下面的事实列表。
- 顺带把这个骨架层功能（GET 读接口 + 完整 Dashboard 标签页 UI）从 `my-live-vps` 分支同步补齐进了这次的开发分支——这两个分支之前在这一块有差异（`my-live-vps` 上已经手动加过 `fact_lookup` 工具和骨架 Dashboard 标签页，但从没同步回开发分支），现在写入功能是在补齐后的基础上一起加的，开发分支和实际部署分支在这一块的功能对齐了。

验证：`python3 -m py_compile server.py` 通过；`node --check` 过了 dashboard.html 里提取出的整个 script 内容；用真实 `FactStore` 模拟了三种写入场景（已注册 predicate 直接写、全新 predicate 先注册再写、`exclusive_current` 新值正确软失效旧值），行为符合预期；`scripts/test_predicate_modes.py` 回归测试仍全部通过。同样准备了一份基于 `my-live-vps` 实际代码改的 server.py + dashboard.html，diff 过确认只多了这一块，其余没有改动。

## 更新：手动加事实的三个补丁——删除、改类型、批量补中文名（同日）

一澜实测后反馈两个问题：①手滑把"生日""结婚纪念日"两个新 predicate 建成了默认的 exclusive_current，需要能改成 historical_event，也希望有删除功能；②默认自带的那 50 个 predicate 在下拉框里显示成"budget_status (budget_status)"这种英文重复，不像她自己新建的那样有中文名——这是因为 `predicate_registry_seed.json` 里的中文名从来没有真正写进过她 VPS 上那份 facts.sqlite（这份种子文件此前只用来对着看，`scripts/seed_predicate_registry.py --apply` 从没在她的实例上跑过）。

补了三个东西：
1. **`FactStore.delete_fact(fact_id)`**：删单条事实记录，`facts_store.py` 新增。
2. **`PATCH /api/facts-skeleton/predicates/{predicate_key}`**：改一个已注册 predicate 的 mode/display_name/notes，只传要改的字段，没传的维持原样——用来把手滑选错的 mode 改过来，不用删掉重建。
3. **`POST /api/facts-skeleton/predicates/backfill-display-names`**：一键把 `predicate_registry_seed.json` 里的中文名/notes 补进已注册但 display_name 还是空的 predicate；只补空的，不碰已经有名字的（包括她自己新建的"生日""结婚纪念日"这种，种子文件里根本没有，不会被误动），可以放心多点几次。
4. **`DELETE /api/facts-skeleton/facts/{fact_id}`**：删单条事实，前端每一条当前值/历史记录后面都跟了一个"✕"。

Dashboard 前端：每个事实分组标题后面加了"改类型"按钮，点开是个小的 mode 下拉框+保存；工具栏加了"补齐默认类型中文名"按钮，点一次就把默认 50 个类型的中文名同步进来。

验证：`python3 -m py_compile server.py` 通过，`node --check` 过 dashboard.html 的 script；用真实 `FactStore` 模拟了 backfill（只补空 display_name、不碰自定义 predicate）、mode 单独修改（display_name/notes 保留原值）、删除已有事实和删除不存在的事实（返回 false）三类场景，行为符合预期；`scripts/test_predicate_modes.py` 回归测试仍全部通过。同样准备了基于 `my-live-vps` 实际代码改的 server.py / dashboard.html / facts_store.py 三个文件，diff 确认只多了这一块。

## 更新："改类型"两次返工——先修 bug，再改位置（同日）

一澜实测"改类型"发现两个问题，一个是 bug，一个是设计位置放错了。

**第一次：bug**——点开一澜"生日"的"改类型"，弹出来的却是林湛"生日"那个下拉框。原因：`predicate_key`（比如 `birthday`）在多个 subject 之间是共用的（predicate_registry 表里 `predicate_key` 是主键，一个 key 只有一行），但前端生成"改类型"编辑框的网页元素 id 只用了 predicate_key，没带 subject_key，导致两个共用同一 key 的分组渲染出了重复 id，`getElementById` 只会抓到文档里第一个匹配的，点哪个都可能弹错。当时的修法是把 id 换成 `subject_key + predicate_key` 拼接，暂时不重复了。

**第二次：一澜追问"类型到底跟着谁走"，指出位置从设计上就放错了**——她说得对：mode 记在 predicate_registry 上，是"这一类事实该怎么处理新旧"的规则，跟具体是谁的、哪一条记录无关；同一个 predicate_key 被几个人共用，改一次就对所有人生效。既然如此，"改类型"就不该长在按 subject 分组显示的事实卡片里（那会让人误以为在单独改某个人的），应该有自己独立的位置，每个 predicate_key 只出现一次。

于是把"改类型"整个搬出来，新增一个独立的"事实类型管理"折叠区（在骨架标签页里，加事实表单下面），列出所有已登记的 predicate_key（按中文名排序），每条一个 mode 下拉框 + 保存按钮，一个 key 只出现一行。原来每个事实分组卡片里的"改类型"按钮/编辑框整个撤掉，`renderFactGroup` 恢复成最初那个纯展示版本。

验证：`python3 -m py_compile server.py` 通过，`node --check` 过 dashboard.html 的 script；`scripts/test_predicate_modes.py` 回归测试仍全部通过。同样准备了基于 `my-live-vps` 实际代码改的 dashboard.html，diff 确认这次只动了 facts-view 区域的这一块（HTML 加了一个折叠区，JS 把改类型相关函数整个替换）。
