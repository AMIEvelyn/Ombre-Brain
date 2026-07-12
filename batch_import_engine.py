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

只输出严格 JSON，不要输出任何其它文字，也不要用 markdown 代码块包裹，格式：
{
  "specific_question": "具体问题或者空字符串",
  "bucket_verdicts": [
    {"bucket_id": "...", "verdict": "in_line", "reason": "一句话原因"}
  ]
}"""

MILESTONE_EXTRACTION_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的里程碑提炼助手。给你一条已经确认属于同一件具体事实、但桶数量较多的记忆桶时间线（比如一个长期项目或者关系历程），从中挑出真正构成这件事"状态变化"的关键节点，不是每个桶都要。

骨架标准（不是死板的三段，是最低要求）：
- 起因：事情为何出现，最初发生了什么。
- 经过：客观状态如何发展；只有发生了有意义的转折才增加节点，不是有桶就加。
- 结果：最后如何结束，留下了什么变化/决定/后续影响。

反复表达"当时很难受"之类、但没有新增客观变化的桶，不需要单独作为节点，但依然要在 dropped_bucket_ids 里列出来——它们仍然属于这条线，只是不构成独立的时间点。

只输出严格 JSON，不要用 markdown 代码块包裹：
{
  "milestone_bucket_ids": ["按时间顺序排好的、真正构成状态变化的桶 id"],
  "dropped_bucket_ids": ["同一条线里给你看过、但不构成独立时间点的桶 id"],
  "reasoning": "一句话说明为什么这样挑"
}"""

CANDIDATE_CARD_SYSTEM_PROMPT = """你是「时光馆」批量导入流程里的候选事实卡生成助手。给你一条已经确认属于同一件具体事实的记忆桶（已经按时间排好，可能是全部桶，也可能是提炼过的关键节点），生成一张候选事实卡。这只是候选，不会自动写入，一澜会人工审核，所以可以标注置信度，但不要为了凑结果编造原文没有的内容。

要求：
1. 标题（title）：具体、稳定、不空泛，一眼看出讲的是什么具体的事，不能是"饮食喜好""我们的关系"这种大类概括。
2. 时间线（revisions）：按时间顺序，每个真正有意义的状态变化各自一条；每条 content 要提炼这个时间点当时的状态，不是原文照抄。**除非这条事实完全不涉及具体某个人（纯粹描述一件物品/地点本身的客观属性），内容要延续记忆桶原本"林湛第一人称"的叙述视角来写**（例如"一澜爱吃酸辣粉"这种主谓句式，不要写成脱离视角的"存在对酸辣粉的偏好"）。
3. 标签（tags）：除了字面相关的词，如果记忆桶原文里反复出现一个和标题字面不同、但明显在指同一个东西的别称/象征说法（例如一件东西本体叫"手串"，但被当"护身符"看待），要把这个别称也写进标签，方便以后按这个别称也能搜到这张卡。
4. 建议文件夹（suggested_folder_paths）：只能从下面提供的"现有文件夹路径"列表里选，可以选多个，也可以一个都不选（如果都不合适，不要编造新路径）。这一步判断的是"这张卡该被归到哪儿"，跟"这是不是同一件事"是两个独立的判断，不要因为归到了同一类文件夹就把标题/内容写得更空泛。
5. confidence（0~1）：对这张卡整体判断的把握程度。

只输出严格 JSON，不要用 markdown 代码块包裹：
{
  "title": "...",
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

        self.pull_line_top_k = int(cfg.get("pull_line_top_k", 30))
        self.large_line_threshold = int(cfg.get("large_line_threshold", 15))
        self.milestone_prefilter_k = int(cfg.get("milestone_prefilter_k", 12))

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url) if self.api_key else None

    async def _call_json(self, system_prompt: str, user_content: str, *, max_tokens: int = 2000) -> dict | None:
        if self.client is None:
            logger.warning("batch_import: no API key configured, skipping LLM call")
            return None
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        if not response.choices:
            return None
        return _parse_json_response(response.choices[0].message.content or "")

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
        result = await self._call_json(
            LINE_JUDGMENT_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
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
            return {"milestone_bucket_ids": [b["id"] for b in shortlist], "dropped_bucket_ids": [], "reasoning": ""}
        result.setdefault("milestone_bucket_ids", [])
        result.setdefault("dropped_bucket_ids", [])
        return result

    # ------------------------------------------------------------------
    # Step ⑤: generate the candidate card
    # ------------------------------------------------------------------
    async def generate_candidate_card(self, line_buckets: list[dict]) -> dict:
        folder_paths = [
            self.card_store.folder_path(f["id"]) for f in self.card_store.list_folders()
        ]
        ordered = sorted(line_buckets, key=lambda b: (b.get("metadata") or {}).get("created", "") or "")
        payload = {
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
                "title": "", "revisions": [], "tags": [],
                "suggested_folder_paths": [], "confidence": 0.0,
                "reasoning": "LLM call failed or returned unparseable output",
            }
        result.setdefault("title", "")
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
            milestone_ids = set(milestones["milestone_bucket_ids"]) or {b["id"] for b in line_buckets}
            candidate_input_buckets = [b for b in line_buckets if b["id"] in milestone_ids]
        else:
            candidate_input_buckets = line_buckets

        candidate_card = await self.generate_candidate_card(candidate_input_buckets)

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
