# ============================================================
# Tests for the recycle bin's server.py wiring (2026-07-19, docs/facts-
# model-v2-collection-redesign.md §14 items 1/7/8): card<->bucket linked
# protection on delete, bucket_restore/bucket_trash_list MCP tools, the
# Dashboard bucket trash/restore/purge endpoints, and the cleanup candidate
# list. Follows the same monkeypatch-server-globals pattern as
# test_memory_api.py.
# ============================================================

import json
import os
import tempfile

import pytest

from cards_store import CardStore


class DummyRequest:
    def __init__(self, body=None, path_params=None, query_params=None):
        self._body = body
        self.headers = {}
        self.path_params = path_params or {}
        self.query_params = query_params or {}

    async def json(self):
        return self._body


class DummyEmbeddingEngine:
    enabled = False

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        return False

    def delete_embedding(self, bucket_id: str):
        return None


def _card_store():
    tmp = tempfile.mkdtemp()
    return CardStore(db_path=os.path.join(tmp, "cards.sqlite"))


@pytest.mark.asyncio
async def test_trace_delete_requires_force_when_bucket_linked_to_card(monkeypatch, bucket_mgr):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    bid = await bucket_mgr.create(content="证据记忆", tags=[], importance=5, domain=["测试"], name="证据")
    cid = store.create_card(title="事实卡")
    store.add_bucket_link(cid, bid)

    blocked = await server.trace(bucket_id=bid, delete=True)
    assert "关联着事实卡" in blocked and "事实卡" in blocked
    assert await bucket_mgr.get(bid) is not None  # not actually deleted

    forced = await server.trace(bucket_id=bid, delete=True, force=True)
    assert "已放入回收站" in forced
    assert await bucket_mgr.get(bid) is None


@pytest.mark.asyncio
async def test_trace_delete_unlinked_bucket_needs_no_force(monkeypatch, bucket_mgr):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    bid = await bucket_mgr.create(content="没关联的记忆", tags=[], importance=5, domain=["测试"], name="X")
    result = await server.trace(bucket_id=bid, delete=True)
    assert "已放入回收站" in result
    assert await bucket_mgr.get(bid) is None


@pytest.mark.asyncio
async def test_read_bucket_surfaces_linked_cards(monkeypatch, bucket_mgr, decay_eng):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)
    monkeypatch.setattr(server, "decay_engine", decay_eng)

    bid = await bucket_mgr.create(content="被引用的记忆", tags=[], importance=5, domain=["测试"], name="Y")
    cid = store.create_card(title="引用它的卡")
    store.add_bucket_link(cid, bid)

    payload = await server.read_bucket(bid)
    assert payload["linked_cards"] == [{"id": cid, "title": "引用它的卡"}]


@pytest.mark.asyncio
async def test_bucket_restore_and_trash_list_mcp_tools(monkeypatch, bucket_mgr):
    import server

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    empty = await server.bucket_trash_list()
    assert "回收站是空的" in empty

    bid = await bucket_mgr.create(content="待恢复内容", tags=[], importance=5, domain=["测试"], name="Z")
    await bucket_mgr.delete(bid)

    listed = await server.bucket_trash_list()
    assert bid in listed and "待恢复内容"[:10] in listed or "Z" in listed

    restored = await server.bucket_restore(bid)
    assert "已恢复" in restored
    assert await bucket_mgr.get(bid) is not None

    missing = await server.bucket_restore("no_such_id")
    assert "没找到" in missing


@pytest.mark.asyncio
async def test_api_buckets_delete_skips_linked_then_force_linked_deletes(monkeypatch, bucket_mgr):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    bid = await bucket_mgr.create(content="批量删除测试", tags=[], importance=5, domain=["测试"], name="W")
    cid = store.create_card(title="批量删除关联卡")
    store.add_bucket_link(cid, bid)

    resp = await server.api_buckets_delete(DummyRequest({"confirm": "DELETE", "bucket_ids": [bid]}))
    body = json.loads(resp.body)
    assert body["skipped"] == 1
    assert body["results"][0]["reason"] == "linked_to_card"
    assert body["results"][0]["linked_cards"] == [{"id": cid, "title": "批量删除关联卡"}]
    assert await bucket_mgr.get(bid) is not None

    resp2 = await server.api_buckets_delete(
        DummyRequest({"confirm": "DELETE", "bucket_ids": [bid], "force_linked": True})
    )
    body2 = json.loads(resp2.body)
    assert body2["deleted"] == 1
    assert await bucket_mgr.get(bid) is None


@pytest.mark.asyncio
async def test_bucket_trash_restore_purge_dashboard_endpoints(monkeypatch, bucket_mgr):
    import server

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    bid = await bucket_mgr.create(content="dashboard 回收站测试", tags=[], importance=5, domain=["测试"], name="V")
    await bucket_mgr.delete(bid)

    resp = await server.api_buckets_trash(DummyRequest())
    body = json.loads(resp.body)
    assert len(body["buckets"]) == 1 and body["buckets"][0]["id"] == bid

    resp2 = await server.api_bucket_restore(DummyRequest(path_params={"bucket_id": bid}))
    assert json.loads(resp2.body)["status"] == "restored"
    assert await bucket_mgr.get(bid) is not None

    await bucket_mgr.delete(bid)
    resp3 = await server.api_bucket_purge(DummyRequest({}, path_params={"bucket_id": bid}))
    assert resp3.status_code == 400  # confirmation required

    resp4 = await server.api_bucket_purge(DummyRequest({"confirm": "DELETE"}, path_params={"bucket_id": bid}))
    assert json.loads(resp4.body)["status"] == "purged"
    assert bucket_mgr.list_trash() == []


@pytest.mark.asyncio
async def test_api_buckets_marks_linked_to_card_without_per_bucket_query(monkeypatch, bucket_mgr):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    linked_id = await bucket_mgr.create(content="关联的桶", tags=[], importance=5, domain=["测试"], name="linked")
    unlinked_id = await bucket_mgr.create(content="没关联的桶", tags=[], importance=5, domain=["测试"], name="unlinked")
    cid = store.create_card(title="引用它的卡")
    store.add_bucket_link(cid, linked_id)

    resp = await server.api_buckets(DummyRequest())
    body = json.loads(resp.body)
    by_id = {b["id"]: b for b in body}
    assert by_id[linked_id]["linked_to_card"] is True
    assert by_id[unlinked_id]["linked_to_card"] is False


@pytest.mark.asyncio
async def test_api_bucket_detail_falls_back_to_trash(monkeypatch, bucket_mgr):
    import server

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", _card_store())
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    bid = await bucket_mgr.create(content="回收站详情页测试", tags=[], importance=5, domain=["测试"], name="详情页")
    await bucket_mgr.delete(bid)

    resp = await server.api_bucket_detail(DummyRequest(path_params={"bucket_id": bid}))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["content"] == "回收站详情页测试"

    resp2 = await server.api_bucket_detail(DummyRequest(path_params={"bucket_id": "no_such_id"}))
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_scheduled_purge_only_removes_entries_past_their_window(monkeypatch, bucket_mgr):
    import server

    store = _card_store()

    # a card past its purge window
    expired_card = store.create_card(title="早该清理的卡")
    store.delete_card(expired_card)
    conn = store._connect()
    conn.execute(
        "UPDATE cards SET deleted_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", expired_card),
    )
    conn.commit()
    conn.close()

    # a card still well within its window
    fresh_card = store.create_card(title="刚删的卡")
    store.delete_card(fresh_card)

    # a bucket past its purge window
    expired_bucket = await bucket_mgr.create(content="早该清理的桶", tags=[], importance=5, domain=["测试"], name="expired")
    await bucket_mgr.delete(expired_bucket)
    trash_file = bucket_mgr._find_trash_file(expired_bucket)
    import frontmatter as fm
    post = fm.load(trash_file)
    post["deleted_at"] = "2020-01-01T00:00:00+00:00"
    with open(trash_file, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    # a bucket still within its window
    fresh_bucket = await bucket_mgr.create(content="刚删的桶", tags=[], importance=5, domain=["测试"], name="fresh")
    await bucket_mgr.delete(fresh_bucket)

    result = await server._run_recycle_bin_purge(bucket_mgr, store)
    assert result == {"cards_purged": 1, "buckets_purged": 1}

    assert store.get_card(expired_card) is None
    assert store.get_card(fresh_card) is not None  # still restorable
    assert bucket_mgr._find_trash_file(expired_bucket) is None
    assert bucket_mgr._find_trash_file(fresh_bucket) is not None  # still restorable

    # idempotent -- nothing left to purge on a second run
    result2 = await server._run_recycle_bin_purge(bucket_mgr, store)
    assert result2 == {"cards_purged": 0, "buckets_purged": 0}


@pytest.mark.asyncio
async def test_cleanup_candidates_excludes_linked_and_unarchived_includes_dormant_archived(
    monkeypatch, bucket_mgr,
):
    import server

    store = _card_store()
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "card_store", store)

    linked_archived = await bucket_mgr.create(content="关联但沉底", tags=[], importance=5, domain=["测试"], name="linked")
    await bucket_mgr.archive(linked_archived)
    cid = store.create_card(title="关联卡")
    store.add_bucket_link(cid, linked_archived)

    unlinked_not_archived = await bucket_mgr.create(content="没沉底", tags=[], importance=5, domain=["测试"], name="fresh")

    unlinked_archived = await bucket_mgr.create(content="沉底且无关联", tags=[], importance=5, domain=["测试"], name="candidate")
    await bucket_mgr.archive(unlinked_archived)

    pinned_archived = await bucket_mgr.create(
        content="钉选保护", tags=[], importance=5, domain=["测试"], name="pinned", pinned=True,
    )
    await bucket_mgr.archive(pinned_archived)

    candidates = await server._select_cleanup_candidate_buckets(max_results=20)
    ids = {b["id"] for b in candidates}
    assert ids == {unlinked_archived}

    rendered = await server.bucket_cleanup_candidates()
    assert unlinked_archived in rendered
    assert linked_archived not in rendered
    assert unlinked_not_archived not in rendered
    assert pinned_archived not in rendered
