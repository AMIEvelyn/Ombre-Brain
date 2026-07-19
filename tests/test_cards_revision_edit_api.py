# ============================================================
# Tests for cards_api.py's revision-level editing (2026-07-19): edit_card
# accepting valid_at, and the new edit_revision/delete_revision endpoints
# that can touch a historical (not just current) timepoint. Same
# FakeMCP/FakeReq harness as test_cards_api.py; kept in its own file since
# test_cards_api.py has an unrelated pre-existing bug partway through its
# single sequential script that stops it from reaching new assertions
# appended at the end.
# ============================================================

import asyncio
import json
import os
import tempfile

import pytest

from cards_store import CardStore, AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN
import cards_api


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods=None):
        def deco(fn):
            for m in methods or []:
                self.routes[(m, path)] = fn
            return fn
        return deco


class FakeReq:
    def __init__(self, path_params=None, query=None, body=None):
        self.path_params = path_params or {}
        self.query_params = query or {}
        self.headers = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _call(mcp, method, path, path_params=None, query=None, body=None):
    # asyncio.run() (not get_event_loop()) -- this file's tests are plain
    # sync functions running alongside other files' @pytest.mark.asyncio
    # tests in the same pytest session; get_event_loop() can hand back an
    # already-closed loop left over from one of those, depending on
    # collection order. asyncio.run() always spins up and cleanly tears
    # down its own loop, so it's immune to that ambient state.
    handler = mcp.routes[(method, path)]
    resp = asyncio.run(handler(FakeReq(path_params, query, body)))
    return resp.status_code, json.loads(bytes(resp.body))


@pytest.fixture
def store():
    tmp = tempfile.mkdtemp()
    return CardStore(db_path=os.path.join(tmp, "cards.sqlite"))


@pytest.fixture
def mcp(store):
    m = FakeMCP()
    cards_api.register_card_routes(m, store, lambda request: None)
    return m


C = "/api/cards-skeleton/cards"


def test_edit_card_accepts_valid_at(mcp, store):
    cid = store.create_card(title="卡", content="内容", valid_at="2026-01-01", author=AUTHOR_YI_LAN)
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid}, body={"valid_at": "2026-02-15"})
    assert st == 200, r
    assert r["card"]["current"]["valid_at"] == "2026-02-15"
    assert r["card"]["current"]["title"] == "卡"  # untouched by a date-only edit


def test_edit_revision_edits_a_historical_timepoint(mcp, store):
    cid = store.create_card(title="标题1", content="内容1", valid_at="2026-01-01", author=AUTHOR_YI_LAN)
    store.add_revision(cid, title="标题2", content="内容2", valid_at="2026-02-01", author=AUTHOR_YI_LAN)
    old_rev_id = next(rv for rv in store.list_revisions(cid) if rv["title"] == "标题1")["id"]

    st, r = _call(
        mcp, "PATCH", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": str(old_rev_id)},
        body={"title": "改过的标题1", "valid_at": "2025-12-25"},
    )
    assert st == 200, r
    old = next(rv for rv in store.list_revisions(cid) if rv["id"] == old_rev_id)
    assert old["title"] == "改过的标题1" and old["valid_at"] == "2025-12-25"
    current = store.get_current_revision(cid)
    assert current["title"] == "标题2"  # the other timepoint is untouched


def test_edit_revision_content_text_and_ownership_conflict(mcp, store):
    cid = store.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    store.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    store.add_revision(cid, content="第二个时间点", author=AUTHOR_YI_LAN, force=True)
    old_rev_id = next(rv for rv in store.list_revisions(cid) if "第二个时间点" not in rv["content"])["id"]

    st, r = _call(
        mcp, "PATCH", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": str(old_rev_id)},
        body={"content_text": "覆盖掉的内容"},
    )
    assert st == 409, r
    assert r["error"] == "ownership_conflict"

    st, r = _call(
        mcp, "PATCH", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": str(old_rev_id)},
        body={"content_text": "覆盖掉的内容", "force": True},
    )
    assert st == 200, r
    old = next(rv for rv in store.list_revisions(cid) if rv["id"] == old_rev_id)
    assert old["content"] == "覆盖掉的内容"


def test_edit_revision_unknown_id_returns_404(mcp, store):
    cid = store.create_card(title="卡", content="内容")
    st, r = _call(
        mcp, "PATCH", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": "999999"}, body={"title": "x"},
    )
    assert st == 404, r


def test_edit_revision_invalid_id_returns_400(mcp, store):
    cid = store.create_card(title="卡", content="内容")
    st, r = _call(
        mcp, "PATCH", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": "not-a-number"}, body={"title": "x"},
    )
    assert st == 400, r


def test_delete_revision_removes_the_timepoint(mcp, store):
    cid = store.create_card(title="标题1", content="内容1", valid_at="2026-01-01")
    store.add_revision(cid, title="标题2", content="内容2", valid_at="2026-02-01")
    old_rev_id = next(rv for rv in store.list_revisions(cid) if rv["title"] == "标题1")["id"]

    st, r = _call(
        mcp, "DELETE", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": str(old_rev_id)},
    )
    assert st == 200, r
    assert len(store.list_revisions(cid)) == 1
    assert store.get_current_revision(cid)["title"] == "标题2"


def test_delete_revision_refuses_the_last_one(mcp, store):
    cid = store.create_card(title="唯一时间点", content="内容")
    only_id = store.get_current_revision(cid)["id"]
    st, r = _call(
        mcp, "DELETE", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": str(only_id)},
    )
    assert st == 400, r
    assert store.get_current_revision(cid) is not None


def test_delete_revision_unknown_id_returns_404(mcp, store):
    cid = store.create_card(title="卡", content="内容")
    store.add_revision(cid, content="第二个时间点")
    st, r = _call(
        mcp, "DELETE", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": cid, "revision_id": "999999"},
    )
    assert st == 404, r


def test_delete_revision_unknown_card_returns_404(mcp, store):
    st, r = _call(
        mcp, "DELETE", C + "/{card_id}/revisions/{revision_id}",
        path_params={"card_id": "no_such_card", "revision_id": "1"},
    )
    assert st == 404, r
