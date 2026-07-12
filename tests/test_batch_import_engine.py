# ============================================================
# Tests for the batch-import pull-a-line engine (docs/batch-import-design.md).
# Uses a scripted fake LLM client so these run with no network/API key and
# exercise the actual decision logic (sweep marking, uncertain handling,
# large-line milestone extraction, never-auto-write-to-CardStore).
# ============================================================

import json
from types import SimpleNamespace

import pytest

from batch_import_engine import CANDIDATE_CARD_SYSTEM_PROMPT, BatchImportEngine
from cards_store import CardStore
from import_progress_store import ImportProgressStore


def test_candidate_card_prompt_forbids_collapsing_a_timeline_into_one_summary():
    """Regression guard: Yi Lan caught that an earlier prompt draft only
    said 'each meaningful change gets its own point' in the abstract, with
    no worked example -- which risks the LLM writing one summary revision
    ('态度前后不一样') instead of one revision per actual state ('喜欢' then
    '讨厌'). The concrete before/after example must stay in the prompt."""
    assert "一澜喜欢吃火锅" in CANDIDATE_CARD_SYSTEM_PROMPT
    assert "一澜现在讨厌吃火锅" in CANDIDATE_CARD_SYSTEM_PROMPT
    assert "禁止这样做" in CANDIDATE_CARD_SYSTEM_PROMPT


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        content = self._responses.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLLMClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


@pytest.fixture
def card_store(test_config, tmp_path):
    return CardStore(test_config, db_path=str(tmp_path / "cards.sqlite"))


@pytest.fixture
def progress_store(test_config, tmp_path):
    return ImportProgressStore(test_config, db_path=str(tmp_path / "import_progress.sqlite"))


@pytest.fixture
def engine(test_config, bucket_mgr, card_store):
    eng = BatchImportEngine(test_config, bucket_mgr, embedding_engine=None, card_store=card_store)
    return eng


def _fixed_pull_line(bucket_mgr, candidate_ids):
    """Replace pull_line with a fixed candidate set so these tests exercise
    the judge/milestone/candidate-generation decision logic in isolation,
    independent of bucket_manager's real BM25/embedding recall ranking
    (that recall behavior is covered separately in test_bucket_cache.py)."""
    async def _pull(seed_bucket):
        return [await bucket_mgr.get(bid) for bid in candidate_ids]
    return _pull


@pytest.mark.asyncio
async def test_small_line_generates_candidate_without_writing_to_cardstore(
    bucket_mgr, engine, progress_store, card_store
):
    seed_id = await bucket_mgr.create(
        content="一澜今天带我去吃酸辣粉，很好吃", tags=[], importance=5,
        domain=["日常"], name="酸辣粉初印象", created="2025-01-01T12:00:00",
    )
    later_id = await bucket_mgr.create(
        content="一澜和我一起吃酸辣粉，她加了很多白糖觉得甜口更好吃", tags=[], importance=5,
        domain=["日常"], name="酸辣粉加糖", created="2025-06-06T12:00:00",
    )
    unrelated_id = await bucket_mgr.create(
        content="路过一家新开的酸辣粉店，只是看了一眼没有进去", tags=[], importance=3,
        domain=["日常"], name="路过酸辣粉店", created="2025-03-01T12:00:00",
    )

    judge_response = json.dumps({
        "specific_question": "对酸辣粉的偏好",
        "bucket_verdicts": [
            {"bucket_id": later_id, "verdict": "in_line", "reason": "同一件事的后续变化"},
            {"bucket_id": unrelated_id, "verdict": "not_related", "reason": "只是路过，没有实际互动"},
        ],
    })
    card_response = json.dumps({
        "title": "对酸辣粉的偏好",
        "revisions": [
            {"content": "一澜爱吃酸辣粉", "valid_at": "2025-01-01", "source_bucket_ids": [seed_id]},
            {"content": "一澜爱吃加糖的酸辣粉", "valid_at": "2025-06-06", "source_bucket_ids": [later_id]},
        ],
        "tags": ["酸辣粉", "饮食偏好"],
        "suggested_folder_paths": [],
        "confidence": 0.9,
        "reasoning": "两个桶讲的是同一件具体偏好随时间的变化",
    })
    engine.client = FakeLLMClient([judge_response, card_response])
    engine.pull_line = _fixed_pull_line(bucket_mgr, [later_id, unrelated_id])

    result = await engine.run_single_line(seed_id, progress_store)

    assert result["status"] == "candidate"
    assert set(result["line_bucket_ids"]) == {seed_id, later_id}
    assert result["candidate_card"]["title"] == "对酸辣粉的偏好"

    # Never auto-written.
    assert card_store.all_cards() == []

    # in_line buckets (+ seed) are swept; the not_related candidate is untouched.
    assert progress_store.is_swept(seed_id)
    assert progress_store.is_swept(later_id)
    assert not progress_store.is_swept(unrelated_id)


@pytest.mark.asyncio
async def test_uncertain_verdict_goes_to_pending_and_stays_unswept(
    bucket_mgr, engine, progress_store
):
    seed_id = await bucket_mgr.create(
        content="一澜说起了小时候的一件事", tags=[], importance=5, domain=["回忆"], name="童年片段",
    )
    maybe_id = await bucket_mgr.create(
        content="一澜又提到了小时候，但这次讲的是完全不同的一件事", tags=[], importance=5,
        domain=["回忆"], name="童年片段2",
    )

    judge_response = json.dumps({
        "specific_question": "",
        "bucket_verdicts": [
            {"bucket_id": maybe_id, "verdict": "uncertain", "reason": "看不出是不是同一件具体的事"},
        ],
    })
    engine.client = FakeLLMClient([judge_response])
    engine.pull_line = _fixed_pull_line(bucket_mgr, [maybe_id])

    result = await engine.run_single_line(seed_id, progress_store)

    assert result["status"] == "pass"
    assert progress_store.is_swept(seed_id)  # seed marked pass, won't be retried as a seed
    assert not progress_store.is_swept(maybe_id)  # uncertain bucket stays fully available

    pending = progress_store.list_pending_associations()
    assert len(pending) == 1
    assert set(pending[0]["bucket_ids"]) == {seed_id, maybe_id}


@pytest.mark.asyncio
async def test_large_line_uses_milestone_extraction(bucket_mgr, engine, progress_store, card_store):
    engine.large_line_threshold = 2  # force the large-line path with just a few buckets

    seed_id = await bucket_mgr.create(
        content="开始写书", tags=[], importance=8, domain=["创作"], name="书-开始",
        created="2025-01-01T00:00:00",
    )
    chat_id = await bucket_mgr.create(
        content="继续聊书的细节，没有新进展", tags=[], importance=3, domain=["创作"], name="书-闲聊",
        created="2025-03-01T00:00:00",
    )
    done_id = await bucket_mgr.create(
        content="书写完了", tags=[], importance=8, domain=["创作"], name="书-完结",
        created="2025-06-01T00:00:00",
    )

    judge_response = json.dumps({
        "specific_question": "写书这件事的进展",
        "bucket_verdicts": [
            {"bucket_id": chat_id, "verdict": "in_line", "reason": "同一本书"},
            {"bucket_id": done_id, "verdict": "in_line", "reason": "同一本书"},
        ],
    })
    milestone_response = json.dumps({
        "milestone_bucket_ids": [seed_id, done_id],
        "dropped_bucket_ids": [chat_id],
        "reasoning": "闲聊没有带来客观状态变化",
    })
    card_response = json.dumps({
        "title": "写书这件事的进展",
        "revisions": [
            {"content": "开始写书", "valid_at": "2025-01-01", "source_bucket_ids": [seed_id]},
            {"content": "书写完了", "valid_at": "2025-06-01", "source_bucket_ids": [done_id]},
        ],
        "tags": ["创作项目"],
        "suggested_folder_paths": [],
        "confidence": 0.85,
        "reasoning": "只保留状态变化的两个节点",
    })
    engine.client = FakeLLMClient([judge_response, milestone_response, card_response])
    engine.pull_line = _fixed_pull_line(bucket_mgr, [chat_id, done_id])

    result = await engine.run_single_line(seed_id, progress_store)

    assert result["status"] == "candidate"
    assert result["dropped_bucket_ids"] == [chat_id]
    assert len(result["candidate_card"]["revisions"]) == 2
    assert card_store.all_cards() == []
    # the "dropped" chat bucket is still part of the line and gets swept too.
    assert progress_store.is_swept(chat_id)


def test_prefilter_for_milestones_bounds_shortlist_size(engine):
    buckets = [
        {
            "id": f"b{i}",
            "metadata": {"created": f"2025-{(i % 12) + 1:02d}-01", "importance": i % 10},
        }
        for i in range(50)
    ]
    shortlist = engine._prefilter_for_milestones(buckets)
    assert len(shortlist) <= engine.milestone_prefilter_k + 4  # date-extremes + importance overlap loosely
    assert len(shortlist) < len(buckets)
