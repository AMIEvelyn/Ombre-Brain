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
from datetime import datetime, timezone

from openai import AsyncOpenAI

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

种子桶（seed）永远是这条线判断的锚点，其它候选桶都是相对种子桶来判断。

候选桶可能有十几二十个，**reason 字段务必简短**（不超过15个字的短语，不要写完整句子），不然输出会太长。

只输出严格 JSON，不要输出任何其它文字，也不要用 markdown 代码块包裹，格式：
{
  "specific_question": "具体问题或者空字符串",
  "bucket_verdicts": [
    {"bucket_id": "...", "verdict": "in_line", "reason": "≤15字短语"}
  ]
}"""

MILESTONE_EXTRACTION_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的里程碑提炼助手。给你一条已经确认属于同一件具体事实、但桶数量较多的记忆桶时间线（比如一个长期项目、身体指标、或者关系历程），任务是把它压缩成几个真正构成"状态变化"的关键节点，不是每个桶都要，但也不能把变化过程压丢。

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

只输出严格 JSON，不要用 markdown 代码块包裹：
{
  "timeline_type": "state 或 event",
  "milestones": [
    {
      "valid_at": "YYYY-MM-DD",
      "role": "initial/change/current（状态线用）或 start/turning_point/result（事件线用）",
      "summary": "这个时间点客观成立的具体状态或事件阶段，提炼过的，不是原文照抄",
      "source_bucket_ids": ["属于这个时间点的全部桶 id，同一天合并的要全列出来"]
    }
  ],
  "dropped_bucket_ids": ["属于同一条线、但不构成独立时间点的桶 id"],
  "reasoning": "一句话说明筛选依据"
}"""

CANDIDATE_CARD_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的候选事实卡生成助手。这只是候选，不会自动写入，一澜会人工审核，所以可以标注置信度，但不要为了凑结果编造原文没有的内容。

你收到的输入有两种模式（看 input_mode 字段）：
- "milestones"：上一步已经把这条线提炼成了几个关键节点（每个节点有 valid_at/summary/source_bucket_ids），你只需要把每个 milestone 转成一条 revision（summary 直接作为 content，可以按下面第3条的视角规则小幅度润色措辞，但不能改变节点已经确定的事实内容和时间点），不需要重新分析原始桶。
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
4. 标签（tags）：除了字面相关的词，如果原文里反复出现一个和标题字面不同、但明显在指同一个东西的别称/象征说法（例如一件东西本体叫"手串"，但被当"护身符"看待），要把这个别称也写进标签，方便以后按这个别称也能搜到这张卡。
5. 建议文件夹（suggested_folder_paths）：只能从下面提供的"现有文件夹路径"列表里选，可以选多个，也可以一个都不选（如果都不合适，不要编造新路径）。这一步判断的是"这张卡该被归到哪儿"，跟"这是不是同一件事"是两个独立的判断，不要因为归到了同一类文件夹就把标题/内容写得更空泛。
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


def _bucket_excerpt(bucket: dict, *, max_chars: int = 400) -> dict:
    meta = bucket.get("metadata", {}) or {}
    content = str(bucket.get("content", "") or "")
    return {
        "bucket_id": bucket.get("id"),
        "date": meta.get("created") or meta.get("date") or meta.get("last_active") or "",
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
        # Unlike dehydration (default "" = provider default), these are
        # structured classification/extraction calls that don't benefit
        # from a visible-or-hidden reasoning pass -- and on models that
        # think by default, that reasoning burns max_tokens invisibly
        # before any JSON gets written, which is what was actually causing
        # truncation, not just "too many candidates". Explicitly off unless
        # overridden.
        self.thinking_mode = str(cfg.get("thinking_mode", "disabled") or "disabled").strip().lower()

        self.pull_line_top_k = int(cfg.get("pull_line_top_k", 30))
        self.large_line_threshold = int(cfg.get("large_line_threshold", 15))
        self.milestone_prefilter_k = int(cfg.get("milestone_prefilter_k", 12))

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url) if self.api_key else None

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
    async def pull_line(self, seed_bucket: dict) -> list[dict]:
        """Seed the associative search with the seed bucket's own content,
        reusing the dual-channel search fixed in §12 rather than a bespoke
        recall path."""
        seed_content = str(seed_bucket.get("content", "") or "")
        seed_name = str((seed_bucket.get("metadata") or {}).get("name", "") or "")
        query = f"{seed_name}\n{seed_content}".strip()[:500]
        matches = await self.bucket_mgr.search_with_semantic(
            query, self.embedding_engine, limit=self.pull_line_top_k, include_archive=True,
        )
        return [b for b in matches if b.get("id") != seed_bucket.get("id")]

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
        """Cheap, free signals first (date extremes / importance / has
        comments) so the LLM only ever sees a small shortlist, not every
        raw bucket in a hundred-plus-bucket line -- see docs §6."""
        by_date = sorted(
            line_buckets,
            key=lambda b: (b.get("metadata") or {}).get("created", "") or "",
        )
        shortlist_ids: set[str] = set()
        k = max(1, self.milestone_prefilter_k // 4)
        for b in by_date[:k]:
            shortlist_ids.add(b["id"])
        for b in by_date[-k:]:
            shortlist_ids.add(b["id"])
        by_importance = sorted(
            line_buckets,
            key=lambda b: int((b.get("metadata") or {}).get("importance", 5)),
            reverse=True,
        )
        for b in by_importance[: self.milestone_prefilter_k // 2]:
            shortlist_ids.add(b["id"])
        for b in line_buckets:
            if (b.get("metadata") or {}).get("comments"):
                shortlist_ids.add(b["id"])
            if len(shortlist_ids) >= self.milestone_prefilter_k:
                break
        return [b for b in line_buckets if b["id"] in shortlist_ids]

    async def extract_milestones(self, line_buckets: list[dict]) -> dict:
        shortlist = self._prefilter_for_milestones(line_buckets)
        payload = {"candidates": [_bucket_excerpt(b) for b in shortlist]}
        result = await self._call_json(
            MILESTONE_EXTRACTION_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )
        if not result:
            # Fallback: treat every shortlisted bucket as its own milestone
            # rather than losing the shortlist entirely.
            return {
                "timeline_type": "state",
                "milestones": [
                    {
                        "valid_at": (b.get("metadata") or {}).get("created", ""),
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
            payload = {
                "input_mode": "milestones",
                "timeline_type": milestones.get("timeline_type", ""),
                "milestones": milestones.get("milestones", []),
                "existing_folder_paths": folder_paths,
            }
        else:
            ordered = sorted(buckets or [], key=lambda b: (b.get("metadata") or {}).get("created", "") or "")
            payload = {
                "input_mode": "buckets",
                "buckets": [_bucket_excerpt(b) for b in ordered],
                "existing_folder_paths": folder_paths,
            }
        result = await self._call_json(
            CANDIDATE_CARD_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=3000,
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
    async def run_single_line(self, seed_bucket_id: str, progress_store) -> dict:
        seed_bucket = await self.bucket_mgr.get(seed_bucket_id)
        if not seed_bucket:
            return {"status": "error", "reason": f"bucket not found: {seed_bucket_id}"}

        try:
            return await self._run_single_line_inner(seed_bucket, seed_bucket_id, progress_store)
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
                "reason": str(e) + " -- 建议调大 config.yaml 里 batch_import.pull_line_top_k 对应的 max_tokens 预算，或减少候选数量后重跑同一个 seed_bucket_id",
            }

    async def _run_single_line_inner(self, seed_bucket: dict, seed_bucket_id: str, progress_store) -> dict:
        candidates = await self.pull_line(seed_bucket)
        judgment = await self.judge_line(seed_bucket, candidates)

        in_line_ids = {
            v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "in_line"
        }
        uncertain_ids = {
            v["bucket_id"] for v in judgment["bucket_verdicts"] if v.get("verdict") == "uncertain"
        }
        by_id = {b["id"]: b for b in candidates}
        by_id[seed_bucket["id"]] = seed_bucket

        if uncertain_ids:
            progress_store.save_pending_association(
                sorted(uncertain_ids | {seed_bucket["id"]}),
                reason="判线拿不准，等待后续证据",
            )

        if not judgment["specific_question"]:
            # No confident line -- mark just the seed as swept (pass) so the
            # sweep doesn't retry the same seed forever, but don't touch the
            # candidates: they're still fully available for their own future line.
            progress_store.mark_swept([seed_bucket["id"]], line_id="")
            return {
                "status": "pass",
                "seed_bucket_id": seed_bucket_id,
                "reasoning": "specific_question 为空，判定为不足以构成一条线",
            }

        # Only trust verdicts for bucket ids that were actually in the
        # candidates we showed the judge -- guards against a hallucinated
        # bucket_id ending up "swept" without ever having been fetched/used.
        line_bucket_ids = sorted((in_line_ids | {seed_bucket["id"]}) & set(by_id))
        line_buckets = [by_id[bid] for bid in line_bucket_ids]

        dropped_bucket_ids: list[str] = []
        if len(line_buckets) > self.large_line_threshold:
            milestones = await self.extract_milestones(line_buckets)
            dropped_bucket_ids = milestones["dropped_bucket_ids"]
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
            reasoning=judgment.get("specific_question", ""),
            confidence=float(candidate_card.get("confidence") or 0.0),
        )
        progress_store.mark_swept(line_bucket_ids, line_id=line_id)

        return {
            "status": "candidate",
            "line_id": line_id,
            "seed_bucket_id": seed_bucket_id,
            "specific_question": judgment["specific_question"],
            "line_bucket_ids": line_bucket_ids,
            "dropped_bucket_ids": dropped_bucket_ids,
            "candidate_card": candidate_card,
        }
