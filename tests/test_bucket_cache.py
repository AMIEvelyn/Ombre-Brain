# ============================================================
# Tests for BucketManager's list_all() in-memory cache and the
# keyword+embedding dual-channel search (docs §12/§13 search-foundation fix).
# ============================================================

import os

import pytest
import frontmatter as fm


def _write_bucket_file_directly(test_config, bucket_id: str, name: str) -> str:
    """Write a bucket .md file straight to disk, bypassing BucketManager --
    simulates Obsidian hand-editing / a migration script."""
    target_dir = os.path.join(test_config["buckets_dir"], "dynamic", "未分类")
    os.makedirs(target_dir, exist_ok=True)
    post = fm.Post(
        f"{name}的正文",
        id=bucket_id,
        name=name,
        tags=[],
        domain=["未分类"],
        valence=0.5,
        arousal=0.3,
        importance=5,
        type="dynamic",
        created="2026-01-01T00:00:00",
        last_active="2026-01-01T00:00:00",
        activation_count=0,
    )
    fpath = os.path.join(target_dir, f"{name}_{bucket_id}.md")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return fpath


@pytest.mark.asyncio
async def test_out_of_band_write_is_invisible_until_invalidate_cache(bucket_mgr, test_config):
    await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    assert len(await bucket_mgr.list_all()) == 1

    _write_bucket_file_directly(test_config, "direct_write_bucket", "B")

    # Cache is warm from the list_all() above -- the file written directly
    # to disk (not through create()) must not appear yet.
    assert len(await bucket_mgr.list_all()) == 1

    bucket_mgr.invalidate_cache()
    assert len(await bucket_mgr.list_all()) == 2


@pytest.mark.asyncio
async def test_create_update_delete_archive_invalidate_cache(bucket_mgr):
    bid_a = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    assert len(await bucket_mgr.list_all()) == 1

    bid_b = await bucket_mgr.create(content="桶B", tags=[], importance=5, domain=["测试"], name="B")
    assert len(await bucket_mgr.list_all()) == 2

    await bucket_mgr.update(bid_a, content="桶A改")
    all_after_update = await bucket_mgr.list_all()
    match = next(b for b in all_after_update if b["id"] == bid_a)
    assert match["content"] == "桶A改"

    await bucket_mgr.delete(bid_b)
    assert len(await bucket_mgr.list_all()) == 1

    await bucket_mgr.archive(bid_a)
    no_archive = await bucket_mgr.list_all(include_archive=False)
    with_archive = await bucket_mgr.list_all(include_archive=True)
    assert len(no_archive) == 0
    assert len(with_archive) == 1


def _cache_timestamps(bucket_mgr) -> dict:
    """Snapshot of list_all()'s internal cache-rebuild timestamps, keyed by
    include_archive. An unchanged timestamp after a write + list_all() call
    proves that call was served from the patched cache, not a real rescan
    (a rescan always sets a fresh time.monotonic() timestamp)."""
    return dict(bucket_mgr._all_buckets_cache_at)


@pytest.mark.asyncio
async def test_create_patches_cache_incrementally_no_full_rescan(bucket_mgr):
    """2026-07-19, Yi Lan's report: the bucket list felt slow, sometimes
    fast sometimes slow -- root cause was every single write blowing away
    the WHOLE list_all() cache, forcing the next call to re-walk and
    re-parse all 7000+ files from disk. create() (and update/comment/
    delete/restore/archive, covered below) now patches the warm cache
    directly instead of invalidating it -- proven here by the cache's
    rebuild timestamp staying put across a write, instead of jumping to a
    fresh time.monotonic() value the way a real rescan would."""
    await bucket_mgr.list_all()  # warm the cache
    before = _cache_timestamps(bucket_mgr)

    bid = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    all_buckets = await bucket_mgr.list_all()

    assert _cache_timestamps(bucket_mgr) == before  # no rebuild happened
    assert [b["id"] for b in all_buckets] == [bid]


@pytest.mark.asyncio
async def test_update_delete_restore_archive_patch_cache_no_full_rescan(bucket_mgr):
    bid_a = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    bid_b = await bucket_mgr.create(content="桶B", tags=[], importance=5, domain=["测试"], name="B")
    await bucket_mgr.list_all(include_archive=True)  # warm both cache slots
    await bucket_mgr.list_all(include_archive=False)
    before = _cache_timestamps(bucket_mgr)

    await bucket_mgr.update(bid_a, content="桶A改")
    match = next(b for b in await bucket_mgr.list_all() if b["id"] == bid_a)
    assert match["content"] == "桶A改"

    await bucket_mgr.delete(bid_b)
    assert [b["id"] for b in await bucket_mgr.list_all()] == [bid_a]

    await bucket_mgr.restore(bid_b)
    ids_after_restore = {b["id"] for b in await bucket_mgr.list_all()}
    assert ids_after_restore == {bid_a, bid_b}

    await bucket_mgr.archive(bid_a)
    no_archive = await bucket_mgr.list_all(include_archive=False)
    with_archive = await bucket_mgr.list_all(include_archive=True)
    assert {b["id"] for b in no_archive} == {bid_b}
    assert {b["id"] for b in with_archive} == {bid_a, bid_b}

    assert _cache_timestamps(bucket_mgr) == before  # every op above stayed cache-only


@pytest.mark.asyncio
async def test_add_and_delete_comment_patch_cache_no_full_rescan(bucket_mgr):
    bid = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    await bucket_mgr.list_all()
    before = _cache_timestamps(bucket_mgr)

    entry = await bucket_mgr.add_comment(bid, "一条年轮", touch=False)
    all_buckets = await bucket_mgr.list_all()
    match = next(b for b in all_buckets if b["id"] == bid)
    assert match["metadata"]["comment_count"] == 1

    await bucket_mgr.delete_comment(bid, entry["id"])
    match = next(b for b in await bucket_mgr.list_all() if b["id"] == bid)
    assert match["metadata"]["comment_count"] == 0

    assert _cache_timestamps(bucket_mgr) == before


@pytest.mark.asyncio
async def test_cache_patch_is_skipped_when_slot_is_cold(bucket_mgr):
    """If a cache slot was never warmed (or was invalidated), the
    incremental patch functions must no-op rather than half-populate it --
    the next real list_all() call builds it correctly from scratch."""
    bid = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    # Cache slots are still cold (list_all() was never called) -- creating
    # a second bucket must not crash or leave a partial cache behind.
    await bucket_mgr.create(content="桶B", tags=[], importance=5, domain=["测试"], name="B")
    assert len(await bucket_mgr.list_all()) == 2


@pytest.mark.asyncio
async def test_search_score_annotation_does_not_leak_into_cache(bucket_mgr):
    """Regression guard: search() writes bucket["score"] onto dicts sourced
    from list_all(). Now that list_all() can serve cached data, those dicts
    must be per-call copies, or a later unrelated list_all() would come back
    with a stale "score" key from a previous, unrelated query."""
    await bucket_mgr.create(
        content="喜欢吃酸辣粉", tags=["饮食"], importance=5, domain=["日常"], name="酸辣粉"
    )
    matches = await bucket_mgr.search("酸辣粉")
    assert matches and "score" in matches[0]

    fresh = await bucket_mgr.list_all()
    assert all("score" not in b for b in fresh)


@pytest.mark.asyncio
async def test_touch_updates_cache_without_invalidating(bucket_mgr, test_config):
    bid = await bucket_mgr.create(content="桶A", tags=[], importance=5, domain=["测试"], name="A")
    assert len(await bucket_mgr.list_all()) == 1

    # Write a second bucket directly to disk after the cache is warm.
    _write_bucket_file_directly(test_config, "direct_write_bucket", "B")

    await bucket_mgr.touch(bid)

    # touch() must patch the cached entry in place, not force a full
    # rebuild -- the out-of-band file from above should still be invisible.
    after_touch = await bucket_mgr.list_all()
    assert len(after_touch) == 1
    touched = next(b for b in after_touch if b["id"] == bid)
    assert touched["metadata"]["activation_count"] == 1


@pytest.mark.asyncio
async def test_search_with_semantic_merges_vector_only_hits(bucket_mgr):
    keyword_id = await bucket_mgr.create(
        content="蓝色星星手链", tags=[], importance=5, domain=["测试"], name="蓝色星星手链"
    )
    vector_id = await bucket_mgr.create(
        content="完全不同的字面表达，讲的是同一件事", tags=[], importance=5,
        domain=["测试"], name="语义相关但字面不同",
    )

    class FakeEmbeddingEngine:
        enabled = True

        async def search_similar(self, query, top_k=10):
            return [(vector_id, 0.9)]

    results = await bucket_mgr.search_with_semantic("手链", FakeEmbeddingEngine(), limit=10)
    ids = {b["id"] for b in results}
    assert keyword_id in ids
    assert vector_id in ids
    vector_bucket = next(b for b in results if b["id"] == vector_id)
    assert vector_bucket.get("vector_match") is True


@pytest.mark.asyncio
async def test_search_with_semantic_falls_back_without_embedding_engine(bucket_mgr):
    bid = await bucket_mgr.create(
        content="蓝色星星手链", tags=[], importance=5, domain=["测试"], name="蓝色星星手链"
    )
    results = await bucket_mgr.search_with_semantic("手链", None, limit=10)
    assert results
    assert results[0]["id"] == bid


@pytest.mark.asyncio
async def test_search_with_semantic_skips_low_score_vector_hits(bucket_mgr):
    vector_id = await bucket_mgr.create(
        content="八竿子打不着的内容", tags=[], importance=5, domain=["测试"], name="不相关",
    )

    class FakeEmbeddingEngine:
        enabled = True

        async def search_similar(self, query, top_k=10):
            return [(vector_id, 0.1)]  # below vector_min_score default (0.5)

    results = await bucket_mgr.search_with_semantic("手链", FakeEmbeddingEngine(), limit=10)
    assert results == []
