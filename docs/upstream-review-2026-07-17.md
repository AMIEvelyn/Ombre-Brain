# 上游 Yinglianchun/Ombre-Brain 更新评估 + 全部同步完成（2026-07-17 ～ 2026-07-18）

写给：一澜、林湛、以及接手的新会话

## ✅ 状态：更新帖里的 9 项全部处理完（2026-07-18 收尾）

一句话版：更新帖提到的东西，该同步的都同步了，该确认"我们已经有、跳过"的也都核实过了，没有遗漏。分两轮做完：

**第一轮（2026-07-17，5 项小同步）**：部署绑定挂载防护、raw memory API 服务鉴权、梦境 Dashboard 直接查看、`bucket_manager.py` 词法评分缓存、`gateway.py` 动态 alpha 置信度校准。

**第二轮（2026-07-18，原计划"留到大改动会话"的 4 项，一澜明确要求这次也一起做完）**：
- **主域判断模型 / semantic_rescue**（`gateway.py`）——默认关闭（`semantic_rescue_enabled: false`），因为触发条件是照着我们自己的 `recall_policy.py` 重新梳理的，不是照抄上游，建议先观察一阵子 debug 再决定开不开。
- **`POST /api/hook/recall`**（`gateway.py`）——给以后可能接的 Codex/Claude Code CLI 用的轻量记忆查询接口，比上游简化了很多，等真有具体工具要接了再按需扩展。
- **照顾备忘 / `reminder_store.py`**（新文件）——独立提醒系统，跟旧的 `todo_store.py`（从记忆桶 `### followup` 派生的待办）并行存在，没有替换关系。3 个 MCP 工具：`reminder_create`/`reminder_list`/`reminder_update`。**注意**：重复规则等纯数据操作走 MCP 就能用，但"到点自动悄悄提醒"这个动作只有走 Gateway 才有，纯 MCP 场景下需要主动调用 `reminder_list` 去查，不会自己冒出来。
- **画像编辑+锁定**（`portrait_engine.py` + `dashboard.html`）——Dashboard 画像页 stable 段加了"编辑"和"锁定"按钮，锁定后夜间自动画像维护不会再改写这个 scope。比上游简化，没做版本历史/回滚。

**全部改动都已提交到 `claude/cards-merge-dedup-tool` 分支并推送**，一澜正在按提示部署上线。详细的逐项技术细节、部署命令见下文各章节。

**下一步**：不再有上游同步相关的待办了。接手会话请直接去 `docs/facts-model-v2-collection-redesign.md` §14 第 5 项（收藏功能收尾）——一澜已经决定下一个会话从那里开始。

---

一澜看到她实际部署所基于的 fork（`github.com/Yinglianchun/Ombre-Brain`，README 自称"Haven/Rain Fork"）作者发的更新帖（3 张截图），想知道能不能把里面提到的更新同步过来，但**明确要求不能碰到时光馆**（`docs/facts-model-v2-collection-redesign.md` 记录的那整套骨架层 v2 系统）。

## 一、先搞清楚关系：这不是简单的"上游有我没有"

`Yinglianchun/Ombre-Brain` 的 README 写明"基于 `P0luz/Ombre-Brain` 二次开发，加入了保守召回、图关系、原文检索、跨窗口 handoff、画像、自我入口、照顾备忘、Darkroom、Dream、自动写入门卫，以及 Gateway"——这些底层能力我们自己的仓库本来就独立有一份（`dream_engine.py`、`portrait_engine.py`、`darkroom.py`、`gateway.py`、`reflection_engine.py`、self_anchor 等）。也就是说**我们和这个 fork 是"同门师兄弟"关系**（大概率共享同一个更早的祖先版本），不是"他有全部我们只有一部分"的单向关系。两边各自独立演化了很久：实测 `gateway.py` 他们 21077 行、我们 15174 行；`portrait_engine.py` 他们 3815 行、我们 2115 行——分叉幅度很大。

**结论（跟 §3 一样，再次确认）**：upstream 代码只能当"参考实现"审查、逐项挑着手动移植，不能指望"复制文件、只是绕开时光馆"这种整体替换的思路——`server.py`/`dashboard.html` 时光馆代码就嵌在文件正中间，没法只拿一半；`gateway.py`/`portrait_engine.py` 虽然没有时光馆代码，但整体替换会把我们自己独立演化出来的几千行东西全部作废，风险比"丢时光馆"更大。

## 二、更新帖 10 项逐条核实（截图内容）

| # | 帖子说的 | 我们这边现状 | 结论 |
|---|---|---|---|
| 1 | 可选开启"主域判断模型" | 实际是 `semantic_rescue`（Gateway 里一个低成本模型二次核验证据的门控），我们没有 | **本轮已同步**（见下），默认关闭待验证 |
| 2 | 新增 hook 端点，Codex/Claude Code 都测过 | 全新的 `POST /api/hook/recall` 子系统（`gateway.py` 新增约15个辅助方法+卡片渲染），我们只有原来的 `/breath-hook` 等三个 | **本轮已同步**（见下），但做了大幅简化，不是照抄 upstream 的大子系统 |
| 3 | favorite_memory | `favorite_tags.py` 逐字节一致，`gateway.py` 的 `_build_favorite_memory_block` 等也对得上 | **已有，跳过** |
| 4 | 照顾备忘（留给下个窗口的话） | upstream 新增独立的 `reminder_store.py`（605行，SQLite，支持重复规则/延后/冷却），**明确替代**了 upstream 旧版"从桶的 `### followup`/`### todo` 派生"的机制；我们现在还是用的这套旧机制（`todo_store.py`，195行） | **本轮已同步**（见下），作为并行的新功能，没有替换旧机制 |
| 5 | 日印象改用原文表，0条门槛也能生成 | `reflection_engine.py` 逻辑、参数名（`daily_min_memory_items`、`conversation_turn_store`）都已一致 | **已有，跳过** |
| 6 | 画像编辑+锁定，后台自动+手动改写并存 | upstream `portrait_engine.py` 有 `edit_stable`/`set_stable_lock`，我们没有这个锁定机制 | **本轮已同步**（见下），做了简化版（没有版本历史/回滚） |
| 7 | 梦境现在可以直接查看了 | 我们原来 `/api/dreams` 的设计就是"只给元数据，正文永不外泄"（docstring 原话），upstream 新增了单独授权的 `/api/dreams/{id}` 才能看正文 | **本轮已同步**（见下） |
| 8 | Gateway"共振"（注入后不销毁） | 检查后确认：这就是我们本来就有的 `dream.inject_enabled`/`retain_after_inject` 机制，代码逐字节一致；帖子大概率是把这个和第7项的新 UI 混在一起说 | **已有，跳过** |
| 9 | Dashboard 支持上游模型热更新，不用手填 config.yaml | 函数名、字段、element id 全部逐字节一致（`_hot_update_gateway_config` 等） | **已有，跳过** |
| 10 | 缓存 + dynamic alpha 置信度/confmargin 校准 | 两件事：① `bucket_manager.py` 新增词法特征缓存（`_lexical_profile_cache`），我们没有；② `gateway.py` 的 `_dynamic_alpha_debug` 校准算法升级（confidence 公式从加权和改乘积、margin 从 top1-top2 改成 top1-avg(top2..top6)），**确认我们现在的版本就是升级前的基线**，改动是一个函数体+6行配置，非常精确 | 值得做，且直接对应"搜索还是慢"那条线索（`docs/facts-model-v2-collection-redesign.md` §14）。**待办**——需要先导出真实 `gateway.py`/`bucket_manager.py` 核对 |

### 顺手挖出来的、帖子没提的东西（翻了 50 条 commit）

- **`bbd6500` 部署绑定挂载防护**（最新一条，HEAD）：Docker 部署时如果宿主机的 `config.yaml`/`.env` 挂载源文件丢了，Docker 会静默把它自动建成一个空**目录**，导致读配置失败且报错完全看不出原因。upstream 加了部署前检查，纯 shell 脚本。**本轮已同步**。
- **`5e0f4f8` raw memory API 允许服务鉴权**：`/api/ingest-raw`、`/api/search-raw` 原来只认 Dashboard 会话登录，upstream 顺手也接受服务 token。我们这边其实已经写好了对应的鉴权函数（`_authorized_memory_write`），就是没接到这两个接口上。**本轮已同步**。
- `d408357` Gateway 里保留 Claude 扩展思考跨工具调用轮次——如果以后 Gateway 走 Claude 工具调用流量，值得单独看一眼，这次没动。
- `9232f4a`/`3748d81` 两个画像相关 bugfix——因为我们没有画像编辑锁定这个功能，这两个改动暂时用不上，等第6项立项时一起看。
- `8ce4219` Darkroom 房间删除——我们目前没有这个能力，小需求，不紧急。
- Operit 相关几个 commit——第三方记忆导出格式的导入器，跟我们无关，跳过。

## 三、本轮已同步的 3 项（自包含、风险低）

全部基于"读 upstream 代码当参考实现，手动适配我们的代码"完成，不是文件替换。测试全绿（新增 4 个 Python 测试 + 1 个 shell 测试，全量回归跑了一遍，跟这次改动无关的 20 个老失败保持不变）。

1. **部署绑定挂载防护**：`scripts/_ops_common.sh` 新增 `ombre_resolve_bind_source`/`ombre_compose_bind_source`/`ombre_validate_compose_file_bind` 三个函数；`scripts/one_click.sh` 的 `backup_current_deployment` 备份真实挂载源、并在容器路径分支末尾跑一次校验；`scripts/update_deploy.sh` 在拉起容器前跑校验。新增 `scripts/test_ops_bind_guard.sh`（原样移植自 upstream，跑起来直接通过，说明我们这几个脚本的结构跟 upstream 依然高度一致）。
2. **raw memory API 服务鉴权**：新增 `_require_raw_api_auth(request)`（`_dashboard_authenticated(request) or _authorized_memory_write(request)`），接到 `api_ingest_raw`/`api_search_raw` 两个路由上，替换原来单纯的 `_require_dashboard_auth`。
3. **梦境 Dashboard 直接查看**：`dream_engine.py` 的 `dashboard_records()` 现在带 `has_body` 标记，新增 `dashboard_record(dream_id)` 返回单条正文；`server.py` 新增 `GET /api/dreams/{dream_id}`（同样走 `_require_dashboard_auth`，只有一澜自己登录能看）；`dashboard.html` 梦境列表的每一行，如果 `has_body` 为真就变成可点开的按钮，点开异步拉正文，收起来不会重复请求。**没有**跟着 upstream 把 `retain_after_inject` 的默认值从 `False` 改成 `True`——那是"梦浮现之后要不要继续留着"的独立行为决定，跟"能不能点开看"这个功能本身没有必然关系，默认值维持我们原来的 `False` 不动，以后想改再单独聊。

**部署**：这次动了 `server.py`、`dream_engine.py`、`dashboard.html`（`ombre-brain` 容器）+ 4 个 `scripts/` 脚本（宿主机，不用 docker cp，直接更新仓库文件）。`scripts/` 那几个不需要重启容器，`server.py`/`dream_engine.py`/`dashboard.html` 需要照常 `docker cp` + `docker restart`。

## 四、本轮同步的第 4、5 项（第10项，动态 alpha 校准 + 召回缓存）

一澜导出了真实部署的 `bucket_manager.py`（`ombre-brain` 容器）和 `gateway.py`（`ombre-gateway` 容器），逐字节核对后确认跟仓库一致，在此基础上完成了这两项：

4. **词法评分缓存**（`bucket_manager.py`）：新增 `_lexical_profile_cache`（按 bucket id 缓存分词结果，签名不匹配时自动失效重算，容量上限 4096 条防止无界增长），`_bucket_lexical_profile()`/`_lexical_phrase_boost()` 都改成共用新的 `_bucket_lexical_cache_entry()`，新增 `warm_lexical_profiles(buckets)` 可以在 `list_all()` 之后批量预热，避免冷启动第一次搜索现算分词。**纯性能优化，不改变任何评分结果**——新增 5 个测试验证缓存命中/失效/预热行为 + 短语加权分值不变。upstream 的 `gateway.py`（`warm_recall_runtime`/`_list_gateway_buckets`）和 `memory_relevance.py`/`recall_policy.py` 部分没有对应移植目标或文件未核对，跳过。
5. **动态 alpha 置信度校准**（`gateway.py` 的 `_dynamic_alpha_debug`）：① confidence 公式从"语义分量*0.7 + margin分量*0.3"改成两者相乘（乘积对"语义分数中等但候选间区分度低"的场景更保守，避免虚高 alpha）；② margin 从"top1 - top2"改成"top1 - avg(top2..top6)"（用更多候选做参照，减少单条候选分数抖动的影响）；③ `conf_lo`/`conf_hi`/`margin_ref`/`alpha_min`/`alpha_max` 全部改成可以从 `recall_thresholds` 里配置覆盖（`dynamic_alpha_conf_lo` 等 5 个新 key），不配置时行为等同于原来的硬编码默认值。`config.example.yaml` 加了这 5 个 key 的注释说明（默认注释掉，不生效），如果实测 alpha 摆动感觉不对可以照着 upstream 给的 Qwen 起始值解注调参。新增 2 个测试锁定新公式的精确数值 + 覆盖行为；已有的 3 个 `dynamic_alpha` 行为测试全部保持通过。

**部署**：这次动了 `bucket_manager.py`（`ombre-brain` 容器）、`gateway.py`（`ombre-gateway` 容器）、`config.example.yaml`（仓库文件，不影响已部署的 `config.yaml`，只是给以后想调参时看的参考）。按老规矩 `docker cp` + 对应容器 `docker restart`。

至此帖子里的 5 项"小同步"（部署防护、raw API 鉴权、梦境查看、词法缓存、alpha 校准）全部完成。

## 五、原计划"留到大改动会话"的 4 项，本轮同步完成

一澜明确要求"剩下的都你做"，这 4 项也在本轮全部完成了。跟小同步不同，这几项都不是"复制粘贴"级别——upstream 的实现依赖大量我们这边没有的内部机制（`recall_policy.py` 的 admission_reason 词汇、`domain_sentinel`、upstream 专属的通用词表等），所以下面每一项都是"读 upstream 代码理解设计意图，再用我们自己已有的构件重新实现"，不是逐行照抄。

6. **主域判断模型 / semantic_rescue**（`gateway.py`）：候选桶有真实语义相似度、但因为缺硬证据（关键词/锚点匹配）被压制时，用一个低成本模型二次核验它的正文是否直接支持当前 query 的某个"激活轴"，命中才放行——而且只认模型指出的原文精确片段，不认标题或分数。我们的 `recall_policy.py` 的 admission_reason 词汇表和 upstream 已经分叉（我们是 `auto_vague_query_without_topic` 这类，upstream 是 `semantic_only`/`no_hard_evidence` 这类），所以 `_semantic_rescue_candidates()` 用的是我们自己重新梳理出的 5 个 reason（`low_recall_evidence`/`query_topic_evidence_missing`/`word_map_topic_evidence_missing`/`non_explicit_query_score_too_low`/`activated_axis_mismatch`——最后一个跟 upstream 逐字一致，确认是共享设计）。**默认关闭**（`semantic_rescue_enabled: false`），等实测过 reason 映射确实可靠再考虑开。挂接点在 `_select_dynamic_buckets`，直接卡片选完之后、还有名额时才触发。
7. **`POST /api/hook/recall`**（`gateway.py`）：给外部工具（以后如果接 Codex/Claude Code CLI 的 hook）用的轻量记忆查询接口，跟聊天走的鉴权一样（Bearer token）。**主动缩小了范围**：upstream 是个 ~40 方法、fast/full 双模式、绑定 `domain_sentinel` 的大子系统，那些机制我们都没有；这边直接复用已经验证过的 `_select_dynamic_buckets` 召回管线，query 进、卡片（bucket_id/title/content/domain/confidence）出，附带一段可以直接注入的 `additional_context` 纯文本。以后真的要接某个具体的 CLI hook 时，照那个 hook 实际要的格式再扩展会比现在瞎猜 upstream 的形状更靠谱。
8. **照顾备忘 / `reminder_store.py`**：全新的独立提醒系统（SQLite），支持一次性/每 N 轮/每天/早晚两次等重复规则，跟"从桶的 `### followup`/`### todo` 派生"的旧机制（`todo_store.py`）是两条并行的路，**这次没有替换旧机制**，只是新增。新增 3 个 MCP 工具（`reminder_create`/`reminder_list`/`reminder_update`）+ 3 个 REST 接口（`GET/POST /api/reminders`、`PATCH /api/reminders/{id}`）+ `gateway.py` 里一个新的"照顾备忘"注入段（跟现有 favorite_memory 段结构一样），回合结束后命中的提醒会自动 `mark_reminded()` 推进重复状态。没有改 `hold`/`grow` 的 docstring 去引导模型别写 `### followup`——upstream 那样做了，但那是"彻底废弃旧机制"的更大决定，这次不做。
9. **画像编辑+锁定**（`portrait_engine.py` + `dashboard.html`）：给 stable 画像段加了 `stable_locked`（bool）和 `stable_revision`（乐观并发用的版本号）。锁定后，夜间 `maintain_daily` 自动改写这个 scope 时会直接跳过（`_apply_patch` 的 `rewrite_stable` 循环里判断），手动编辑（`edit_stable`）不受锁定影响、可以随时改。新增 `edit_stable(scope, text, expected_revision, locked=None)`（改文字+可选顺带切换锁定）和 `set_stable_lock(scope, locked, expected_revision)`（只切换锁定，不动文字）两个方法；`PATCH /api/portrait-state/{scope}/stable` 和 `POST /api/portrait-state/{scope}/lock` 两个 REST 接口；Dashboard 画像页 stable 段现在带"✏️ 编辑"和"🔒 锁定/解锁"按钮。**比 upstream 简单**：没有做 `stable_history`/`rollback_stable`（版本历史+回滚），只做了她明确要的"编辑+锁定"本身；`stable_revision` 只是防冲突用的计数器，不存历史快照。老的 state 文件里没有这两个新字段，加载时会自动按 `False`/`0` 补上（`_merge_state` 已有的合并逻辑），不需要额外迁移脚本。

**部署**：这次动了 `gateway.py`（`ombre-gateway` 容器）、`reminder_store.py`（新文件，`ombre-brain` 容器）、`server.py`、`portrait_engine.py`、`dashboard.html`（`ombre-brain` 容器）。按老规矩 `docker cp` + 对应容器 `docker restart`。`reminder_store.py`/`portrait_engine.py` 都已确认跟真实部署逐字节一致后才动的刀。
