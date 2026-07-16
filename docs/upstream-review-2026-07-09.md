# 上游 P0luz/Ombre-Brain 能力评估 + 一处安全补丁（2026-07-09）

写给：一澜、林湛、以及接手的新会话

一澜看到原作者 P0luz 的 upstream（现已到 v2.4.6 / v2.5.0）做了些升级，让我评估对我们有没有帮助、能不能顺手取长补短。结论：**绝大部分我们已经有了或明确不要，只有一处安全洞是我们真缺的、且是小活，已当场补上。** 其余 upstream 新东西都是大活，记录在这里。

**2026-07-16 更新**：下面"三、留到项目完结后再议"第1条提到的 **v2.5 性能优化（记忆缓存、并发脱水、BM25 后台重建）**，跟当前正在跟进的"搜索还是慢"这个问题（`docs/facts-model-v2-collection-redesign.md` §14）很可能是同一类瓶颈，值得优先拿出来看一眼，别当成普通的"以后再说"事项埋掉。

## 一、已完成：hook 端点鉴权（堵住一个真实的公网信息泄露）

**问题**：`/breath-hook` 和 `/dream-hook`（`server.py`）此前**完全没有鉴权**。这两个端点会把记忆浮现内容原样吐出来——核心准则、长期锚点、未解决的记忆。一澜的 18001 端口对公网开放（为了 ChatGPT/Claude 连接器走 MCP），所以任何人 GET 一下 `/breath-hook` 就能读到她的记忆。这正是 upstream 的 `hooks.token` + `allow_public: false` 要堵的洞。

**为什么对使用零影响**：一澜和林湛用的 MCP 连接走的是 `/mcp` 端点，`breath` 是作为 MCP 工具调用的，跟 `/breath-hook` 是**两条完全独立的代码路径**。这两个 hook 端点的唯一合法调用者是本地 Claude Code 的 SessionStart 钩子脚本（`.claude/hooks/session_breath.py`），`gateway.py` 内部不调它们。所以给 hook 加鉴权，`/mcp` 一行不动，她和林湛没有任何感觉。

**改法（默认关闭，可 token 放行）**：
- `server.py` 新增 `_hook_auth_ok(request)`，`/breath-hook` 和 `/dream-hook`(+`/introspection-hook`) 顶部各加一句校验。
  - 配了 token（`OMBRE_HOOK_TOKEN` 环境变量，或 `config.yaml` 的 `hooks.token`，env 优先）→ 需用 `?token=` / `Authorization: Bearer` / `X-Ombre-Hook-Token` 携带正确 token 才放行，用 `hmac.compare_digest` 常数时间比较。
  - 没配 token → 看 `hooks.allow_public`，默认 `false` → 一律 401（安全默认关闭）。
- `config.example.yaml` 新增 `hooks:` 段（`token: ""` + `allow_public: false`）。
- `.claude/hooks/session_breath.py` 改成会自动把 `OMBRE_HOOK_TOKEN` 作为 `?token=` 带上——这样真用本地钩子的人，设个 token 就能继续用。
- `README.md` 的 `.env` 示例加了 `OMBRE_HOOK_TOKEN`。

**一澜要不要做额外操作**：不用。她走 `/mcp`，不依赖这两个 hook。补丁上线后这两个端点对公网默认 401，MCP 照常。**只有一种情况**需要她设 `OMBRE_HOOK_TOKEN`：如果她在某处确实靠本地 SessionStart 钩子来做开场 breath——那就在服务端 `.env` 和跑钩子的环境里设同一个 token 值即可。

**验证**：`python3 -m py_compile server.py .claude/hooks/session_breath.py` 通过；`config.example.yaml` YAML 解析通过；`_hook_auth_ok` 的逻辑用隔离脚本跑了完整真值表（默认关闭 / allow_public 放行 / config token 对错 / Bearer / X-Ombre-Hook-Token / env token 覆盖 config 且 allow_public 不能绕过已设 token），10 条断言全过。**没跑真实 HTTP 端到端**（沙盒依赖，见下方备注），部署前建议在真实环境 curl 一下 `/breath-hook`（不带 token 应 401，带对的 token 应 200）确认。

> 部署铁律照旧：发给一澜部署的文件必须基于 `my-live-vps` 基线。本次改动就是在 `my-live-vps`（与她真实部署逐字节一致）上做的，涉及文件：`server.py`、`.claude/hooks/session_breath.py`、`config.example.yaml`、`README.md`。

## 二、明确不取

- **多身份隔离 / Multi-Owner**（`OMBRE_OWNER_NAME` / `OMBRE_OWNER_COUNT`，一个 OB 兼容多个 AI 人格）：一澜明确不要，是"一心一意"的相反方向，还会把 gateway 的 session/persona 路由搞复杂。
- **"I" self-awareness 工具**（AI 写/读"我是什么"，不随普通 breath 浮现、开场自动附最近 3 条）：这就是我们 `self_anchor`（自我入口）的同一个想法。我们的 self_anchor + handoff/portrait 体系比它更完整，不需要再引入一个平行概念。
- **OAuth 2.1 / Dashboard 密码**：我们 fork 早就有（`OMBRE_DASHBOARD_PASSWORD`、ChatGPT/Claude Connector OAuth）。

## 三、留到项目完结后再议（大活）

这些不是小复制粘贴，牵扯多文件或架构，且我们的基线跟 upstream v2.4.6 分叉很大，硬抄有风险。价值排序供以后参考：

1. **v2.5 性能优化**：记忆缓存（memory caching）、并发脱水（concurrent dehydration）、BM25 后台重建（background rebuild）。对 2C4G 的机器、记忆桶攒多以后有实际意义，但要动检索/脱水核心链路，属于大活。**优先级最高的一项**，等骨架层收尾后可以单独立项评估。
2. **Dashboard 内置 Cloudflare Tunnel 连接器**（免命令行开公网）：便利性功能，非安全刚需（她现在的反代 HTTPS + 防火墙已够用），改动落在 Dashboard，中等偏大。
3. 其余 upstream 变化（v2.4 架构、非商用声明等）与我们无关或已覆盖。

**注意**：真要取上面任何一项时，先对着我们 `my-live-vps` 的实际 `server.py`/`gateway.py`/`dehydrator.py` 看能不能落，把 upstream 代码当"参考实现"审查，不当"现成代码"照搬——他都 v2.4.6 了，文件跟我们对不上。

## 备注：沙盒依赖

这次没跑真实 HTTP 端到端是因为默认沙盒缺 `jieba`/`mcp`/`httpx` 等。按之前会话记录的经验，装依赖先试 `pip install --no-build-isolation` + 降 `setuptools<60`，很可能就通了；通了之后可以完整 `import server` 并跑 `scripts/test_predicate_modes.py`。本次改动很小且自包含，用 py_compile + 逻辑真值表验证已足够，但部署前在真实环境 curl 一下最稳。
