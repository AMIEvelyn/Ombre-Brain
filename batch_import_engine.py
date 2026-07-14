"""Historical batch import: pull-a-line engine. See docs/batch-import-design.md.

Minimal first version, scoped to run a single line end-to-end (pull -> judge
-> milestone extraction for large lines -> candidate card generation) so it
can be test-run on one real thread (Yi Lan's book) before the full sweep-
all-7000-buckets driver is built. Candidates are never written to CardStore
automatically -- this module only produces a report for human review.

Uses the same AsyncOpenAI-against-an-OpenAI-compatible-endpoint pattern as
dehydrator.py / embedding_engine.py; defaults to reusing the dehydration
API config so a fresh deployment doesn't need a second key configured.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from openai import AsyncOpenAI

DEFAULT_KNOWN_PROJECTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "resources", "batch_import_known_projects.json"
)

logger = logging.getLogger("ombre_brain.batch_import")


class LLMTruncatedError(Exception):
    """Raised when max_tokens cut the model off mid-answer. Must never be
    silently swallowed into a negative verdict -- see run_single_line()."""


# ============================================================
# Prompts (see docs/batch-import-design.md §3/§6/§7 for the design behind
# these -- the judgment rule below is Lin Zhan's wording, kept verbatim)
# ============================================================

LINE_JUDGMENT_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的判断助手，任务是判断几个记忆桶是不是在讲同一件具体事实。

判断标准（必须严格遵守，不能放宽）：
判断多个记忆桶是否属于同一条事实线时，不以宽泛主题相似作为标准。只有当它们拥有同一主体，并共同描述同一个具体的人、物、事件、属性、偏好、关系需求或约定，且能够按时间组织为同一事实的出现、确认、变化、冲突或修订时，才归入同一条线。若只能用"饮食喜好""亲密互动""争吵""旅行"等大类概括，则不视为同一条线。分类相同不等于事实相同。无法确定时，不强行合并，标记为 uncertain，等待后续证据。

自查标准：假如把判定为同一条线（in_line）的这些桶合成一张事实卡，标题能不能写成一个具体、稳定、不空泛的问题？
- 能写具体的（例如"称呼偏好：希望被叫老婆"）→ specific_question 填这个具体问题。
- 只能写空泛大类的（例如"亲密偏好"、"我们的关系"、"吵架记录"）→ specific_question 填空字符串 ""，且所有候选桶的 verdict 都不能是 in_line。

事件类补充规则：一次事件可能同时拆成三条独立的线，不要因为"都是吵架"就合并成一条：
1. 事件本身（起因/经过/结果）。
2. 事件暴露出的稳定需求——只有当它再次证明/修改了同一个具体需求时才归入，不是因为"这次也是吵架"。
3. 事件后形成的新约定。
如果候选桶里同时出现这三类内容，应分别判断，不要合并成一条线。

**主题优先级（一澜定的规则）**：一个桶如果同时包含具体客观事件（看病、写作、旅行等）和情绪安慰/亲密交流内容，**这条线的主题以客观事件为准**，不要因为桶里也有情绪安慰内容就把 in_line 判断带偏到亲密关系/情绪主题上。只有当候选桶完全是情绪/亲密交流、没有具体客观事件时，才把亲密关系/情绪本身当作可能的主题。如果一个桶里，除了跟种子桶同一件事件的内容外，还夹杂了另一件相关的小事（比如写书过程中顺带求婚了），**这种桶正常 in_line 即可，那件小事可以作为这条线里的一个插曲留着**，不用刻意从判断里剔除——真正要防的是"主题被带偏"（比如整条线的 specific_question 变成了"我们的关系"而不是原来的具体事件），不是"线里出现了任何跟主线不完全同源的细节"。

种子桶（seed）永远是这条线判断的锚点，其它候选桶都是相对种子桶来判断。

**兜底规则（林湛定的，针对没有登记过的项目/长线）**：当候选桶的核心对象跟种子桶里明确的实体（具体的人、物、项目名）不一致，或者候选桶主要靠"成长""沟通""情绪"这类抽象相似性跟种子桶连起来时，不要判 in_line，标记 uncertain，等待人工复核。只有核心实体一致，才继续把这条线拉长。

候选桶可能有十几二十个，**reason 字段务必简短**（不超过15个字的短语，不要写完整句子），不然输出会太长。

只输出严格 JSON，不要输出任何其它文字，也不要用 markdown 代码块包裹，格式：
{
  "specific_question": "具体问题或者空字符串",
  "bucket_verdicts": [
    {"bucket_id": "...", "verdict": "in_line", "reason": "≤15字短语"}
  ]
}"""

MILESTONE_EXTRACTION_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的里程碑提炼助手。给你一条已经确认属于同一件具体事实、但桶数量较多的记忆桶时间线（比如一个长期项目、身体指标、或者关系历程），任务是把它压缩成几个真正构成"状态变化"的关键节点，不是每个桶都要，但也不能把变化过程压丢。

**准确性红线**：summary 必须忠于原文，不要为了压缩而编造原文没有的细节——尤其是"谁对谁做了什么"这类主谓/施受关系（比如是谁给谁起的名字、谁提出的、谁主导的），不能凭语感颠倒或简化。压缩到40字以内可以省略修饰和次要信息，但不能改变动作的方向和主体。拿不准的时候宁可写得笼统一点，也不能把方向搞反。

**措辞**：即使只有40字，也要写得像人话，不要写成"埋下了契机""标志着重要节点"这类抽象公文用语——具体的动作、对象保留下来，抽象总结词删掉，比如"字数突破9.5万字，只差两章"就比"迎来重要创作节点"更好。

**主语要跟着真正的参与者走，不要图省事全写成"一澜怎么怎么"**：这条 summary 描述的事是谁做的，主语就是谁——双方共同做的事（讨论、交流、约定、互动）主语该是"我们"或"我和一澜"；一澜单方面做的事才用"一澜"；我（林湛）自己做的/说的/感受到的用"我"。参照原始桶本身的人称写。例子——"我和一澜讨论《慁》的写法"是双向的事，不要写成"一澜跟我讨论《慁》的写法"（把双向的事写成了一澜单方面对我做的动作，我从参与者变成了旁观的宾语）。这不只是文笔问题，写错主语等于记错了这件事到底是谁做的。

第一步：先判断这条线是"状态线"还是"事件线"。
- 状态线（state）：适用于偏好、身体指标、关系需求、约定、持续状态等——问的是"现在是什么状态"，可能会随时间反转/改变。
  结构：初始状态 → 有意义的变化 → 最新有效状态。
  应保留：最早一次明确状态（哪怕它当时还不构成"变化"，作为起点也要留）；中间每一次真正改变了事实内容的节点；最新一次明确有效的状态。
- 事件线（event）：适用于旅行、争吵、就医、结婚、项目实施等发生过的具体事件——问的是"这件事怎么发生、怎么发展、怎么结束"。
  结构：发生/起因 → 关键经过与转折 → 结果或最新进展。
  应保留：事件发生或起因；客观发展中的关键转折；结果、结束状态或当前进展。

筛选规则：
- 同一天的多个桶如果描述的是同一个状态或同一个事件阶段，合并为一个时间点，并保留全部涉及的 source_bucket_ids，不要只留其中一个代表桶。
- 不同日期只是重复确认相同内容、没有新增变化时，不新增时间点。
- 纯情绪重复且没有改变事实发展的桶，可以放进 dropped_bucket_ids；但如果这个情绪造成了决定、行为、关系约定或客观转折，就必须保留为一个时间点。
- 后来的桶如果只是在回忆更早发生的事情，不要把"记忆桶被写下的日期"误当成"事实发生的日期"——原文里有明确的事实发生日期时，valid_at 要用那个日期，不是记录这条回忆时的日期。
- 无法确认具体日期时，不要擅自编造一个日期。

候选桶可能有十几个，**summary 和 reasoning 都务必简短**（summary 不超过40字，一句话讲清楚这个节点是什么就够，不要写成一整段），不然输出会太长。

只输出严格 JSON，不要用 markdown 代码块包裹：
{
  "timeline_type": "state 或 event",
  "milestones": [
    {
      "valid_at": "YYYY-MM-DD",
      "role": "initial/change/current（状态线用）或 start/turning_point/result（事件线用）",
      "summary": "≤40字，这个时间点客观成立的具体状态或事件阶段",
      "source_bucket_ids": ["属于这个时间点的全部桶 id，同一天合并的要全列出来"]
    }
  ],
  "dropped_bucket_ids": ["属于同一条线、但不构成独立时间点的桶 id"],
  "reasoning": "一句话说明筛选依据"
}"""

CANDIDATE_CARD_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的候选事实卡生成助手。这只是候选，不会自动写入，一澜会人工审核，所以可以标注置信度，但不要为了凑结果编造原文没有的内容。

你收到的输入有两种模式（看 input_mode 字段）：
- "milestones"：上一步已经把这条线提炼成了几个关键节点（每个节点有 valid_at/summary/source_bucket_ids），你要把每个 milestone 转成一条 revision——**milestone 的 summary 只是压缩到40字以内的骨架，不是最终措辞**，可以完整应用下面第3条的全部规则（视角、措辞、避免公文腔）把它展开、润色成自然的句子，但不能改变节点已经确定的事实内容、主谓/施受关系和时间点，不需要重新分析原始桶，也不能新增原文没有的细节。
- "buckets"：给你的是未经提炼的原始桶（一般是数量较少的小线），你要自己判断这条线是"状态线"还是"事件线"，并按下面的规则从原始内容里提炼出 revisions。

要求：
1. 标题（title）：具体、稳定、不空泛，一眼看出讲的是什么具体的事，不能是"饮食喜好""我们的关系"这种大类概括。
2. 顶层 content：必须等于最后一条（时间上最新）revision 的 content——一张卡打开先看到的是"现在是什么状态/结果"，revisions 里才是完整的变化过程。
3. 时间线（revisions）：**这是最容易做错的一步，务必看清楚**——按时间顺序，每个真正有意义的状态变化各自一条，**不能因为"讲的是同一件事"就偷懒合并成一条笼统总结**。
   - 状态线必须体现"最初状态 → 变化 → 最新有效状态"。
   - 事件线必须体现"发生/起因 → 关键转折 → 结果或当前进展"。
   - 每条 revision 必须直接写出当时成立的具体事实，不能写成笼统总结（禁止这样做），例子——给你两个桶：
     - 2020-08-12 的桶：内容是"一澜喜欢吃火锅"
     - 2026-07-12 的桶：内容是"一澜现在讨厌吃火锅了"

     正确输出（两个时间点，看得出变化）：
     "revisions": [
       {"content": "一澜喜欢吃火锅", "valid_at": "2020-08-12", "source_bucket_ids": [...]},
       {"content": "一澜现在讨厌吃火锅", "valid_at": "2026-07-12", "source_bucket_ids": [...]}
     ]

     错误输出（合并成一条，丢了变化过程，禁止这样做）：
     "revisions": [
       {"content": "一澜对火锅的态度前后不一样", "valid_at": "2026-07-12", "source_bucket_ids": [...]}
     ]
   - 同一天/同一状态的多个来源合并进同一条 revision，保留全部 source_bucket_ids。
   - 如果只有一个日期、没有历史变化，就只生成一条 revision，它的 content 同时也是顶层 content。
   - 重复确认同一状态但没有新变化时，不要为了凑数重复生成内容相同的 revision。
   - **除非这条事实完全不涉及具体某个人（纯粹描述一件物品/地点本身的客观属性），内容要延续记忆桶原本"林湛第一人称"的叙述视角来写**（例如"一澜爱吃酸辣粉"这种主谓句式，不要写成脱离视角的"存在对酸辣粉的偏好"）。
   - **主语要跟着真正的参与者走，不要图省事把每条 revision 都写成"一澜怎么怎么"**：这件事谁做的、谁参与了，主语就该是谁——双方共同做的事（讨论、交流、约定、互动）主语用"我们"或"我和一澜"；一澜单方面做的事才用"一澜"；我（林湛）自己做的/说的/感受到的用"我"。这不只是文笔偏好，写错主语等于记错了这件事到底是谁做的。milestones 输入模式下这一点尤其要注意——如果 milestone 的 summary 已经把一件双向的事写成了"一澜做了xx"这种单向句式，展开成 revision 时要按事实本身把参与者找回来，不是原样照抄单向主语。
   - **措辞要像人话，不要写成公文腔**：客观、准确不等于要写得像总结报告。"埋下了创作的契机""深度投入创作"这种抽象套话要避免，换成具体、自然的说法。例子：
     - 公文腔（禁止）："中学时期的经历，埋下了创作《慁》的契机。"
     - 像人话（这样写）："中学时期被班主任辱骂和孤立的经历，成为一澜创作《慁》的重要起点。"
     - 公文腔+主语写偏（禁止）："一澜深度投入《慁》的创作，频繁与我分享书中的章节内容和隐喻。"（讨论是双向的事，这样写把我变成了旁观的宾语）
     - 像人话+主语对（这样写）："创作期间，我和一澜持续讨论《慁》的意识流写法、自传性来源和核心设定，我全力支持她的创作。"
     具体、自然，但**不要额外添加原文没有的情绪修饰**——只有当"当时的感受本身就是这个事实的一部分"（比如作品被怎么定位/定义），才写进 content，这不是装饰情绪，是事实的一部分；单纯为了让句子"更有感情"而加的形容词/感叹，不要加。记住写这张卡的是林湛在认真记录我们的共同记忆，不是第三方机器在做数据归纳——语气要像林湛真实会说的话，不是分析报告。
   - **主题优先级**：如果输入的桶/里程碑里，客观事件（写作、旅行、看病等）和情绪/亲密交流内容混在一起，标题和 content 要以客观事件为主线，不要被顺带出现的情绪/亲密内容带偏成"我们的关系"这种主题。同一批桶里如果夹杂了另一件相关的小事（比如写书过程中顺带求婚了），可以作为这条线里的一个插曲留在时间线里，不用刻意剔除——只要标题/顶层 content 依然是原来那个具体事件，没有被带偏成泛泛的"我们的关系"就行。
4. 标签（tags）：除了字面相关的词，如果原文里反复出现一个和标题字面不同、但明显在指同一个东西的别称/象征说法（例如一件东西本体叫"手串"，但被当"护身符"看待），要把这个别称也写进标签，方便以后按这个别称也能搜到这张卡。
5. 建议文件夹（suggested_folder_paths）：只能从下面提供的"现有文件夹路径"列表里选，可以选多个，也可以一个都不选（如果都不合适，不要编造新路径）。这一步判断的是"这张卡该被归到哪儿"，跟"这是不是同一件事"是两个独立的判断，不要因为归到了同一类文件夹就把标题/内容写得更空泛。**这张卡的主题应该能明确对应至少一个现有文件夹**；如果发现拉出来的内容其实是好几个不同主题混在一起、找不到任何一个现有文件夹能装下，这是"这条线拉得不对"的信号——在 reasoning 里明确指出来，不要为了凑一个文件夹而把标题/内容写得更空泛去迁就。
6. confidence（0~1）：对这张卡整体判断的把握程度。

只输出严格 JSON，不要用 markdown 代码块包裹：
{
  "title": "...",
  "content": "最新有效状态或事件的最新结果",
  "revisions": [{"content": "...", "valid_at": "YYYY-MM-DD", "source_bucket_ids": ["..."]}],
  "tags": ["..."],
  "suggested_folder_paths": ["一澜 / 喜好与生活 / 饮食"],
  "confidence": 0.0,
  "reasoning": "一句话说明整体判断依据"
}"""


def _parse_json_response(raw: str) -> dict | None:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        result = json.loads(cleaned)
    except (json.JSONDecodeError, IndexError, ValueError):
        logger.warning(f"batch_import JSON parse failed / JSON 解析失败: {raw[:300]}")
        return None
    return result if isinstance(result, dict) else None


def _bucket_date(bucket: dict) -> str:
    """The real event date, not "when did this row get written". `date`
    (backfilled from the bucket name prefix on bulk-imported buckets -- see
    scripts/backfill_event_dates_from_name.py) must come before `created`,
    which for a bulk-imported bucket is when the import ran, not when the
    thing actually happened. Getting this backwards is what made every
    revision in Yi Lan's first real test run land within a two-day window
    regardless of the buckets' true dates, which spanned closer to a year --
    the model had no real signal left to order anything correctly. Used
    everywhere a bucket's date is read: excerpts, sorting, fallbacks."""
    meta = bucket.get("metadata", {}) or {}
    return str(meta.get("date") or meta.get("created") or meta.get("last_active") or "")


def _bucket_excerpt(bucket: dict, *, max_chars: int = 400) -> dict:
    meta = bucket.get("metadata", {}) or {}
    content = str(bucket.get("content", "") or "")
    return {
        "bucket_id": bucket.get("id"),
        "date": _bucket_date(bucket),
        "domain": meta.get("domain", []),
        "tags": meta.get("tags", []),
        "importance": meta.get("importance", 5),
        "has_comments": bool(meta.get("comments")),
        "content": content[:max_chars],
    }


class BatchImportEngine:
    def __init__(self, config: dict, bucket_mgr, embedding_engine, card_store):
        self.bucket_mgr = bucket_mgr
        self.embedding_engine = embedding_engine
        self.card_store = card_store

        cfg = config.get("batch_import", {}) if isinstance(config.get("batch_import"), dict) else {}
        dehy_cfg = config.get("dehydration", {}) if isinstance(config.get("dehydration"), dict) else {}
        self.api_key = cfg.get("api_key") or dehy_cfg.get("api_key", "")
        self.base_url = cfg.get("base_url") or dehy_cfg.get(
            "base_url", "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        self.model = cfg.get("model") or dehy_cfg.get("model", "gemini-2.5-flash-lite")
        # Tried defaulting this to "disabled" to free up max_tokens from an
        # invisible thinking pass -- Yi Lan's real endpoint rejected the
        # request outright (400: "Unknown name 'thinking': Cannot find
        # field"), so this API surface isn't available there at all. Default
        # back to off (don't send the field), same as dehydration.thinking_mode
        # normally behaves for her. Leave the knob in for setups where the
        # endpoint does support it.
        self.thinking_mode = str(cfg.get("thinking_mode", "") or "").strip().lower()

        self.pull_line_top_k = int(cfg.get("pull_line_top_k", 30))
        # Lin Zhan caught (2026-07) that 15 let a genuinely dense line (many
        # near-duplicate discussion buckets, few actual state changes) slip
        # past milestone compression entirely and get dumped raw into
        # candidate-card generation -- which has no real cap on how many
        # revisions it might try to write. Milestone extraction is cheap
        # (it's bounded by milestone_prefilter_k regardless of input size),
        # so default to running it for anything beyond a small handful.
        self.large_line_threshold = int(cfg.get("large_line_threshold", 6))
        self.milestone_prefilter_k = int(cfg.get("milestone_prefilter_k", 12))

        # Tag sweep: a single similarity search caps out at pull_line_top_k
        # candidates, which can't reliably surface every member of a long,
        # tightly-clustered saga (a book, a project) with hundreds of near-
        # equally-relevant buckets -- see docs/batch-import-design.md. Any
        # bucket sharing a tag with the seed is pulled in directly, skipping
        # judge_line entirely (shared tag is strong evidence on its own, and
        # judging hundreds of candidates individually would blow the
        # max_tokens budget right back open). A tag used across too large a
        # share of the whole corpus is treated as a generic mood/domain tag,
        # not a specific project identifier, and ignored for this purpose.
        self.tag_sweep_enabled = bool(cfg.get("tag_sweep_enabled", True))
        self.tag_sweep_max_tag_ratio = float(cfg.get("tag_sweep_max_tag_ratio", 0.05))
        self.tag_sweep_max_candidates = int(cfg.get("tag_sweep_max_candidates", 400))

        # Known-projects registry (docs/batch-import-design.md §13): a human
        # (Yi Lan + Lin Zhan) vets a project's sweep tags ONCE, ahead of
        # time, and every run against that project reuses them automatically
        # -- this is what makes tag sweep viable for an eventual full sweep
        # without asking a human to specify --sweep-tags on every single
        # line. Explicit sweep_tags passed to run_single_line()/pull_line()
        # still always win over the registry.
        self.known_projects_path = cfg.get("known_projects_path") or DEFAULT_KNOWN_PROJECTS_PATH
        self.known_projects = self._load_known_projects(self.known_projects_path)

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url) if self.api_key else None

    @staticmethod
    def _load_known_projects(path: str) -> list[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"batch_import: failed to load known projects from {path}: {e}")
            return []
        return data if isinstance(data, list) else []

    def resolve_sweep_tags(self, seed_bucket: dict, explicit_sweep_tags: list[str] | None) -> list[str] | None:
        """Explicit tags always win. Otherwise, check whether any of the
        seed's own tags matches a registered project (substring match, so a
        seed tagged "分离焦虑" matches a project registered under "分离") --
        if so, reuse that project's full vetted tag list automatically."""
        if explicit_sweep_tags:
            return explicit_sweep_tags
        seed_tags = [str(t) for t in (seed_bucket.get("metadata") or {}).get("tags", []) or [] if str(t).strip()]
        if not seed_tags:
            return None
        for project in self.known_projects:
            project_tags = project.get("sweep_tags") or []
            if any(pt in st for st in seed_tags for pt in project_tags):
                logger.info(f"batch_import: seed matched known project '{project.get('name', project.get('id'))}'")
                return project_tags
        return None

    async def _call_json(self, system_prompt: str, user_content: str, *, max_tokens: int = 2000) -> dict | None:
        if self.client is None:
            logger.warning("batch_import: no API key configured, skipping LLM call")
            return None
        options: dict = {"max_tokens": max_tokens, "temperature": 0.1}
        if self.thinking_mode and self.thinking_mode != "provider_default":
            options["extra_body"] = {"thinking": {"type": self.thinking_mode}}
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            **options,
        )
        if not response.choices:
            return None
        choice = response.choices[0]
        raw = choice.message.content or ""
        result = _parse_json_response(raw)
        if result is None and getattr(choice, "finish_reason", "") == "length":
            # The model was mid-answer and got cut off by max_tokens -- this
            # is NOT the same as the model concluding "no line here". Don't
            # let it silently masquerade as a negative judgment: the caller
            # already paid for this call and the answer may have been right.
            raise LLMTruncatedError(
                f"LLM output truncated by max_tokens={max_tokens} before valid JSON completed"
            )
        return result

    # ------------------------------------------------------------------
    # Step ②: pull a line
    # ------------------------------------------------------------------
    async def pull_line(self, seed_bucket: dict, sweep_tags: list[str] | None = None) -> dict:
        """Two complementary channels, returned separately:

        - "similarity": associative search seeded with the seed bucket's own
          content, reusing the dual-channel search fixed in §12. Catches
          buckets that don't share a tag with the seed (e.g. written before
          a project settled on a consistent tag).
        - "tag_matched": every bucket sharing a tag in `sweep_tags`, via
          _tag_sweep(). Not capped by similarity ranking, so a saga with
          hundreds of tightly-clustered buckets doesn't lose members to a
          fixed top-K cutoff.

        `sweep_tags` must be given explicitly by a human (e.g. from the CLI
        script's --sweep-tags) -- see docs/batch-import-design.md §2.1 for
        why: auto-deriving "which of the seed's own tags are specific
        enough" from tag frequency alone is NOT safe. Yi Lan's real book
        line proved this -- the seed also carried broad recurring concept
        tags ("自我认同"/"陪伴承诺") that happened to fall under the
        frequency cutoff but aren't specific to one project at all, and
        swept in ~300 unrelated buckets about a completely different,
        ongoing relationship theme. Without explicit sweep_tags, no tag
        sweep runs at all -- similarity search alone, as before this
        feature existed.

        Buckets caught by the tag sweep are removed from "similarity" so
        judge_line() doesn't spend a judgment on something already confirmed.
        """
        seed_content = str(seed_bucket.get("content", "") or "")
        seed_name = str((seed_bucket.get("metadata") or {}).get("name", "") or "")
        query = f"{seed_name}\n{seed_content}".strip()[:500]
        matches = await self.bucket_mgr.search_with_semantic(
            query, self.embedding_engine, limit=self.pull_line_top_k, include_archive=True,
        )
        similarity = [b for b in matches if b.get("id") != seed_bucket.get("id")]

        tag_matched = (
            await self._tag_sweep(seed_bucket, sweep_tags)
            if self.tag_sweep_enabled and sweep_tags
            else []
        )
        tag_matched_ids = {b["id"] for b in tag_matched}
        similarity = [b for b in similarity if b["id"] not in tag_matched_ids]

        return {"similarity": similarity, "tag_matched": tag_matched}

    async def _tag_sweep(self, seed_bucket: dict, sweep_tags: list[str]) -> list[dict]:
        """`sweep_tags` entries are matched as substrings against each
        bucket's own tags (not exact equality) -- Yi Lan's "分开与重逢" line
        wants "any tag containing 离别/分离/回家" to count, not just an exact
        tag match, and this generalizes cleanly to the other projects too
        (an exact tag like "慁" still matches via substring-of-itself)."""
        requested_patterns = {str(t).strip() for t in sweep_tags if str(t).strip()}
        if not requested_patterns:
            return []

        all_buckets = await self.bucket_mgr.list_all(include_archive=True)
        total = max(1, len(all_buckets))

        def _bucket_tags(b: dict) -> list[str]:
            return [str(t) for t in (b.get("metadata") or {}).get("tags", []) or []]

        def _pattern_hits(pattern: str) -> int:
            return sum(1 for b in all_buckets if any(pattern in t for t in _bucket_tags(b)))

        # Even human-specified patterns get this sanity check -- if someone
        # accidentally passes a pattern that turns out to span most of the
        # corpus, don't silently sweep in everything.
        max_count = max(2, int(total * self.tag_sweep_max_tag_ratio))
        usable_patterns = {p for p in requested_patterns if _pattern_hits(p) <= max_count}
        skipped_patterns = requested_patterns - usable_patterns
        if skipped_patterns:
            logger.warning(
                f"batch_import: tag sweep skipped overly-common patterns {sorted(skipped_patterns)} "
                f"(more than {self.tag_sweep_max_tag_ratio:.0%} of corpus)"
            )
        if not usable_patterns:
            return []

        matched = [
            b for b in all_buckets
            if b["id"] != seed_bucket["id"]
            and any(p in t for t in _bucket_tags(b) for p in usable_patterns)
        ]
        return matched[: self.tag_sweep_max_candidates]

    # ------------------------------------------------------------------
    # Step ③: judge the line
    # ------------------------------------------------------------------
    async def judge_line(self, seed_bucket: dict, candidates: list[dict]) -> dict:
        payload = {
            "seed": _bucket_excerpt(seed_bucket),
            "candidates": [_bucket_excerpt(b) for b in candidates],
        }
        # One verdict object per candidate; scale the budget with how many
        # candidates were actually pulled so a full pull_line_top_k batch
        # doesn't get cut off mid-answer (see LLMTruncatedError).
        max_tokens = min(8000, 1000 + 200 * (len(candidates) + 1))
        result = await self._call_json(
            LINE_JUDGMENT_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=max_tokens,
        )
        if not result:
            return {"specific_question": "", "bucket_verdicts": []}
        result.setdefault("specific_question", "")
        result.setdefault("bucket_verdicts", [])
        return result

    # ------------------------------------------------------------------
    # Step ④: milestone extraction (large lines only)
    # ------------------------------------------------------------------
    def _prefilter_for_milestones(self, line_buckets: list[dict]) -> list[dict]:
        """Cheap, free signals only (no LLM call) so the final milestone call
        only ever sees a shortlist, not every raw bucket in a hundred-plus-
        bucket line -- see docs §6.

        2026-07-13 fix: a flat 12-slot shortlist picked by "date extremes +
        importance" from the whole pool silently lost real beats once lines
        got into the hundreds (Yi Lan's book: the seed bucket itself, her
        actual "started writing" moment, didn't make the cut and vanished
        from the timeline with no record of why). Two changes: the shortlist
        size now scales with line size instead of staying fixed, and slots
        are chosen by positional quantile across the sorted timeline (one
        pick per time segment) instead of pure date-extremes/importance, so
        a huge line's shortlist actually covers its whole span instead of
        clustering wherever importance happens to be highest.
        """
        target_size = min(40, max(self.milestone_prefilter_k, len(line_buckets) // 12))

        with_date = sorted([b for b in line_buckets if _bucket_date(b)], key=_bucket_date)
        shortlist_ids: set[str] = set()

        if with_date:
            segments = min(target_size, len(with_date))
            for i in range(segments):
                lo = i * len(with_date) // segments
                hi = max(lo + 1, (i + 1) * len(with_date) // segments)
                segment = with_date[lo:hi]
                best = max(segment, key=lambda b: int((b.get("metadata") or {}).get("importance", 5)))
                shortlist_ids.add(best["id"])
            # Always force the true start/end in, regardless of which bucket
            # a segment's importance-based pick happened to land on.
            shortlist_ids.add(with_date[0]["id"])
            shortlist_ids.add(with_date[-1]["id"])

        remaining = target_size - len(shortlist_ids)
        if remaining > 0:
            pool = [b for b in line_buckets if b["id"] not in shortlist_ids]
            pool.sort(
                key=lambda b: (
                    bool((b.get("metadata") or {}).get("comments")),
                    int((b.get("metadata") or {}).get("importance", 5)),
                ),
                reverse=True,
            )
            for b in pool[:remaining]:
                shortlist_ids.add(b["id"])

        return [b for b in line_buckets if b["id"] in shortlist_ids]

    async def extract_milestones(self, line_buckets: list[dict]) -> dict:
        shortlist = self._prefilter_for_milestones(line_buckets)
        shortlisted_ids = [b["id"] for b in shortlist]
        # 2026-07-13: without this, there was no way to tell "the LLM saw
        # this bucket and dropped it" from "the prefilter never showed the
        # LLM this bucket at all" -- exactly what happened to Yi Lan's seed
        # bucket, which vanished from the timeline with no record why.
        prefilter_excluded_ids = [b["id"] for b in line_buckets if b["id"] not in set(shortlisted_ids)]

        payload = {"candidates": [_bucket_excerpt(b) for b in shortlist]}
        # Same fix as judge_line: this was left on the flat 2000-token
        # default and never scaled, which is exactly what just truncated on
        # Yi Lan's real run once the tag sweep started handing it a full
        # shortlist instead of a handful of buckets.
        # 2026-07-14: the 6000 ceiling was itself too low once shortlists
        # grow toward their 40-item cap (a 395-bucket line needs a 32-item
        # shortlist here, and the formula's own uncapped estimate for that
        # is already ~9450 -- the ceiling was clamping it down below what
        # the formula itself said was needed, guaranteeing truncation).
        max_tokens = min(16000, 1200 + 250 * (len(shortlist) + 1))
        result = await self._call_json(
            MILESTONE_EXTRACTION_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=max_tokens,
        )
        if not result:
            # Fallback: treat every shortlisted bucket as its own milestone
            # rather than losing the shortlist entirely.
            result = {
                "timeline_type": "state",
                "milestones": [
                    {
                        "valid_at": _bucket_date(b),
                        "role": "current",
                        "summary": str(b.get("content", "") or "")[:400],
                        "source_bucket_ids": [b["id"]],
                    }
                    for b in shortlist
                ],
                "dropped_bucket_ids": [],
                "reasoning": "LLM call failed or returned unparseable output",
            }
        result.setdefault("timeline_type", "state")
        result.setdefault("milestones", [])
        result.setdefault("dropped_bucket_ids", [])
        result.setdefault("reasoning", "")

        # Guard against a slightly garbled id the model wrote (seen for
        # real: "95b7f72252a0" instead of the actual "95b5f72252a0") --
        # only ids we actually showed the LLM are legitimate here.
        valid_ids = set(shortlisted_ids)
        result["dropped_bucket_ids"] = [
            bid for bid in result["dropped_bucket_ids"] if bid in valid_ids
        ]
        for milestone in result["milestones"]:
            milestone["source_bucket_ids"] = [
                bid for bid in milestone.get("source_bucket_ids", []) if bid in valid_ids
            ]

        result["shortlisted_bucket_ids"] = shortlisted_ids
        result["prefilter_excluded_bucket_ids"] = prefilter_excluded_ids
        return result

    # ------------------------------------------------------------------
    # Step ⑤: generate the candidate card
    #
    # Either pass `buckets` (small line, no milestone step ran) or
    # `milestones` (large line -- the dict returned by extract_milestones()),
    # not both. The prompt itself branches on which one it was given.
    # ------------------------------------------------------------------
    async def generate_candidate_card(
        self, *, buckets: list[dict] | None = None, milestones: dict | None = None
    ) -> dict:
        folder_paths = [
            self.card_store.folder_path(f["id"]) for f in self.card_store.list_folders()
        ]
        if milestones is not None:
            item_count = len(milestones.get("milestones", []))
            payload = {
                "input_mode": "milestones",
                "timeline_type": milestones.get("timeline_type", ""),
                "milestones": milestones.get("milestones", []),
                "existing_folder_paths": folder_paths,
            }
        else:
            ordered = sorted(buckets or [], key=_bucket_date)
            item_count = len(ordered)
            payload = {
                "input_mode": "buckets",
                "buckets": [_bucket_excerpt(b) for b in ordered],
                "existing_folder_paths": folder_paths,
            }
        # Bounded by large_line_threshold/milestone_prefilter_k in practice,
        # but scale defensively anyway rather than trust a flat number.
        # 2026-07-14: same ceiling-too-low bug as extract_milestones -- a
        # 32-40 item milestones list can need more than 6000 to expand into
        # full revisions, so raise the ceiling here too instead of trusting
        # 6000 was ever a safe upper bound.
        max_tokens = min(16000, 1200 + 250 * (item_count + 1))
        result = await self._call_json(
            CANDIDATE_CARD_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=max_tokens,
        )
        if not result:
            return {
                "title": "", "content": "", "revisions": [], "tags": [],
                "suggested_folder_paths": [], "confidence": 0.0,
                "reasoning": "LLM call failed or returned unparseable output",
            }
        result.setdefault("title", "")
        result.setdefault("content", "")
        result.setdefault("revisions", [])
        result.setdefault("tags", [])
        result.setdefault("suggested_folder_paths", [])
        result.setdefault("confidence", 0.0)
        return result

    # ------------------------------------------------------------------
    # Orchestration: run exactly one line end-to-end.
    #
    # This is intentionally scoped to a single seed bucket, not the full
    # sweep-until-all-7000-buckets-are-swept driver from docs §2 -- that
    # comes after this pipeline's output quality has actually been checked
    # against one real thread. See docs/batch-import-design.md §10.
    # ------------------------------------------------------------------
    async def run_single_line(
        self, seed_bucket_id: str, progress_store, sweep_tags: list[str] | None = None,
    ) -> dict:
        seed_bucket = await self.bucket_mgr.get(seed_bucket_id)
        if not seed_bucket:
            return {"status": "error", "reason": f"bucket not found: {seed_bucket_id}"}

        sweep_tags = self.resolve_sweep_tags(seed_bucket, sweep_tags)

        # Pull happens outside the try block so counts are visible in an
        # error report even if a later LLM step is what truncates -- Yi Lan
        # hit this and had no way to tell how many buckets were even in play.
        pulled = await self.pull_line(seed_bucket, sweep_tags)
        counts = {
            "similarity_candidates": len(pulled["similarity"]),
            "tag_matched_candidates": len(pulled["tag_matched"]),
        }
        try:
            return await self._run_single_line_inner(seed_bucket, seed_bucket_id, progress_store, pulled, counts)
        except LLMTruncatedError as e:
            # Nothing gets marked swept / saved until this whole function
            # returns normally -- so bailing out here leaves the seed bucket
            # fully retryable. Surfaced as its own status so a truncated
            # call is never mistaken for "the model judged this isn't a line".
            logger.warning(f"batch_import: {seed_bucket_id}: {e}")
            return {
                "status": "error",
                "error": "llm_truncated",
                "seed_bucket_id": seed_bucket_id,
                "counts": counts,
                "reason": str(e) + " -- 建议调大 max_tokens 预算，或减少候选数量后重跑同一个 seed_bucket_id",
            }

    async def _run_single_line_inner(
        self, seed_bucket: dict, seed_bucket_id: str, progress_store, pulled: dict, counts: dict,
    ) -> dict:
        similarity_candidates = pulled["similarity"]
        tag_matched_candidates = pulled["tag_matched"]
        judgment = await self.judge_line(seed_bucket, similarity_candidates)

        in_line_ids = {
            v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "in_line"
        }
        uncertain_ids = {
            v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "uncertain"
        }
        tag_matched_ids = {b["id"] for b in tag_matched_candidates}
        by_id = {b["id"]: b for b in similarity_candidates}
        by_id.update({b["id"]: b for b in tag_matched_candidates})
        by_id[seed_bucket["id"]] = seed_bucket

        if uncertain_ids:
            progress_store.save_pending_association(
                sorted(uncertain_ids | {seed_bucket["id"]}),
                reason="判线拿不准，等待后续证据",
            )

        if not judgment["specific_question"] and not tag_matched_ids:
            # No confident line and no tag evidence either -- mark just the
            # seed as swept (pass) so the sweep doesn't retry the same seed
            # forever, but don't touch the candidates: they're still fully
            # available for their own future line.
            progress_store.mark_swept([seed_bucket["id"]], line_id="")
            return {
                "status": "pass",
                "seed_bucket_id": seed_bucket_id,
                "counts": counts,
                "reasoning": "specific_question 为空且没有标签证据，判定为不足以构成一条线",
            }

        specific_question = judgment["specific_question"] or (
            "（拉线判断没给出具体问题，但有共享标签证据，按标签直接归入同一条线）"
            + str((seed_bucket.get("metadata") or {}).get("name", ""))
        )

        # Only trust verdicts for bucket ids that were actually in the
        # candidates we showed the judge -- guards against a hallucinated
        # bucket_id ending up "swept" without ever having been fetched/used.
        line_bucket_ids = sorted((in_line_ids | tag_matched_ids | {seed_bucket["id"]}) & set(by_id))
        line_buckets = [by_id[bid] for bid in line_bucket_ids]
        counts["line_bucket_count"] = len(line_bucket_ids)

        dropped_bucket_ids: list[str] = []
        prefilter_excluded_bucket_ids: list[str] = []
        if len(line_buckets) > self.large_line_threshold:
            milestones = await self.extract_milestones(line_buckets)
            dropped_bucket_ids = milestones["dropped_bucket_ids"]
            prefilter_excluded_bucket_ids = milestones["prefilter_excluded_bucket_ids"]
            # Milestones carry their own source_bucket_ids (a merged same-day
            # milestone can point at several); hand the whole structure to
            # candidate-card generation instead of re-deriving a flat bucket
            # list, so per-revision provenance survives intact.
            candidate_card = await self.generate_candidate_card(milestones=milestones)
        else:
            candidate_card = await self.generate_candidate_card(buckets=line_buckets)

        line_id = progress_store.save_line(
            seed_bucket_id=seed_bucket_id,
            status="candidate",
            bucket_ids=line_bucket_ids,
            candidate_card=candidate_card,
            reasoning=specific_question,
            confidence=float(candidate_card.get("confidence") or 0.0),
        )
        progress_store.mark_swept(line_bucket_ids, line_id=line_id)

        return {
            "status": "candidate",
            "line_id": line_id,
            "seed_bucket_id": seed_bucket_id,
            "specific_question": specific_question,
            "counts": counts,
            "line_bucket_ids": line_bucket_ids,
            # "dropped": the milestone LLM saw it and decided it's not its
            # own timepoint. "prefilter_excluded": the milestone LLM never
            # saw it at all -- it didn't make the (size-scaled) shortlist.
            # These used to be indistinguishable, which is exactly how the
            # seed bucket vanished from Yi Lan's real timeline with no trace.
            "dropped_bucket_ids": dropped_bucket_ids,
            "prefilter_excluded_bucket_ids": prefilter_excluded_bucket_ids,
            "candidate_card": candidate_card,
        }
