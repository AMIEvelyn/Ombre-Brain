# ============================================================
# Tests for BucketManager's recycle bin (2026-07-19, docs/facts-model-v2-
# collection-redesign.md §14 item 7): delete() moves the whole .md file
# into trash_dir instead of destroying its content, restore() moves it
# back, purge_from_trash() is the real irreversible removal.
# ============================================================

import os

import pytest


@pytest.mark.asyncio
async def test_delete_preserves_full_content_in_trash(bucket_mgr):
    bid = await bucket_mgr.create(
        content="完整的原始内容，不能丢", tags=["t1"], importance=5, domain=["测试"], name="待删除",
    )
    assert await bucket_mgr.delete(bid)

    # gone from normal listing/lookup
    assert await bucket_mgr.get(bid) is None
    assert len(await bucket_mgr.list_all(include_archive=True)) == 0

    # but the trash entry still has the real content, not a tombstone stub
    trash = bucket_mgr.list_trash()
    assert len(trash) == 1
    assert trash[0]["id"] == bid
    assert trash[0]["content_preview"] == "完整的原始内容，不能丢"
    assert trash[0]["deleted_at"]
    assert trash[0]["purge_at"]

    # the tombstone json (read by sync_to_supabase.py) is still written,
    # exactly as before this change
    tombstone_path = os.path.join(bucket_mgr.tombstone_dir, f"{bid}.json")
    assert os.path.exists(tombstone_path)


@pytest.mark.asyncio
async def test_restore_brings_back_original_content_and_location(bucket_mgr):
    bid = await bucket_mgr.create(
        content="要恢复的内容", tags=["t1"], importance=7, domain=["情感"], name="待恢复",
    )
    await bucket_mgr.delete(bid)
    assert await bucket_mgr.restore(bid)

    bucket = await bucket_mgr.get(bid)
    assert bucket is not None
    assert bucket["content"] == "要恢复的内容"
    assert bucket["metadata"].get("importance") == 7
    assert "deleted_at" not in bucket["metadata"]

    # tombstone removed -- the id is alive again
    tombstone_path = os.path.join(bucket_mgr.tombstone_dir, f"{bid}.json")
    assert not os.path.exists(tombstone_path)

    # back in normal listing
    assert bid in {b["id"] for b in await bucket_mgr.list_all()}
    assert bucket_mgr.list_trash() == []


@pytest.mark.asyncio
async def test_restore_nonexistent_returns_false(bucket_mgr):
    assert await bucket_mgr.restore("no_such_bucket_id") is False


@pytest.mark.asyncio
async def test_purge_from_trash_is_irreversible(bucket_mgr):
    bid = await bucket_mgr.create(content="彻底删除测试", tags=[], importance=5, domain=["测试"], name="C")
    await bucket_mgr.delete(bid)
    assert len(bucket_mgr.list_trash()) == 1

    assert await bucket_mgr.purge_from_trash(bid)
    assert bucket_mgr.list_trash() == []
    assert await bucket_mgr.restore(bid) is False  # nothing left to restore

    # purge doesn't touch the tombstone -- the id stays reported as deleted
    tombstone_path = os.path.join(bucket_mgr.tombstone_dir, f"{bid}.json")
    assert os.path.exists(tombstone_path)


@pytest.mark.asyncio
async def test_purge_from_trash_nonexistent_returns_false(bucket_mgr):
    assert await bucket_mgr.purge_from_trash("no_such_bucket_id") is False


@pytest.mark.asyncio
async def test_delete_preserves_domain_subfolder_on_restore(bucket_mgr, test_config):
    bid = await bucket_mgr.create(
        content="域测试", tags=[], importance=5, domain=["旅行"], name="域测试桶",
    )
    original_path = bucket_mgr._find_bucket_file(bid)
    assert os.path.join(bucket_mgr.dynamic_dir, "旅行") in original_path

    await bucket_mgr.delete(bid)
    await bucket_mgr.restore(bid)

    restored_path = bucket_mgr._find_bucket_file(bid)
    assert restored_path == original_path  # exact same location, not just same domain name
