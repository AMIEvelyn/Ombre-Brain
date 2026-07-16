"""End-to-end tests for cards_api.register_card_routes.

Drives the real handlers (via a fake FastMCP that captures routes and a fake
Starlette-ish Request) against a real CardStore on a temp sqlite db. Needs
starlette (for JSONResponse). Run: `python3 tests/test_cards_api.py`.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards_store import CardStore  # noqa: E402
import cards_api  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.routes = {}  # (method, path) -> handler

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
    handler = mcp.routes[(method, path)]
    resp = asyncio.get_event_loop().run_until_complete(
        handler(FakeReq(path_params, query, body))
    )
    return resp.status_code, json.loads(bytes(resp.body))


def main():
    tmp = tempfile.mkdtemp()
    store = CardStore(db_path=os.path.join(tmp, "cards.sqlite"))
    mcp = FakeMCP()
    cards_api.register_card_routes(mcp, store, lambda req: None)  # auth always passes

    C = "/api/cards-skeleton/cards"
    F = "/api/cards-skeleton/folders"

    # --- folders: create a tree ---
    st, r = _call(mcp, "POST", F, body={"name": "我们"})
    assert st == 200, r
    subj = r["folder"]["id"]
    st, r = _call(mcp, "POST", F, body={"name": "旅行", "parent_id": subj})
    travel = r["folder"]["id"]
    st, r = _call(mcp, "POST", F, body={"name": "香港", "parent_id": travel})
    hk = r["folder"]["id"]
    st, r = _call(mcp, "POST", F, body={"name": "林湛收藏", "is_favorite": True})
    fav = r["folder"]["id"]
    # bad parent
    st, r = _call(mcp, "POST", F, body={"name": "x", "parent_id": "nope"})
    assert st == 400, r
    # missing name
    st, r = _call(mcp, "POST", F, body={})
    assert st == 400, r
    print("PASS folders create + validation")

    # --- create a card filed into hk + fav ---
    st, r = _call(mcp, "POST", C, body={"title": "香港迪士尼", "content": "出发",
                                        "valid_at": "2026-03-01", "folder_ids": [hk, fav]})
    assert st == 200, r
    cid = r["card"]["id"]
    assert cid.startswith("F")
    assert r["card"]["current"]["content"] == "出发"
    # empty card rejected
    st, r = _call(mcp, "POST", C, body={})
    assert st == 400, r
    print("PASS card create + validation")

    # --- attachment count cap: 10 per type, image/file counted separately ---
    def _atts(n_images, n_files):
        return ([{"type": "image", "url": f"/x/i{i}.png"} for i in range(n_images)]
                + [{"type": "file", "url": f"/x/f{i}.md"} for i in range(n_files)])

    st, r = _call(mcp, "POST", C, body={"title": "附件测试", "attachments": _atts(10, 10)})
    assert st == 200, r  # exactly at the cap is fine
    st, r = _call(mcp, "POST", C, body={"title": "附件超限", "attachments": _atts(11, 0)})
    assert st == 400 and "图片附件最多" in r["error"], r
    st, r = _call(mcp, "POST", C, body={"title": "附件超限", "attachments": _atts(0, 11)})
    assert st == 400 and "文件附件最多" in r["error"], r
    # 10 images + 10 files together is fine -- caps are independent, not combined
    st, r = _call(mcp, "POST", C, body={"title": "附件测试2", "attachments": _atts(10, 10)})
    assert st == 200, r
    print("PASS attachment count cap (per-type, independent)")

    # edit/new-revision paths enforce the same cap
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"attachments": _atts(11, 0)})
    assert st == 400 and "图片附件最多" in r["error"], r
    st, r = _call(mcp, "POST", C + "/{card_id}/revisions", path_params={"card_id": cid},
                  body={"attachments": _atts(0, 11)})
    assert st == 400 and "文件附件最多" in r["error"], r
    print("PASS attachment count cap enforced on edit + new revision too")

    # --- get card ---
    st, r = _call(mcp, "GET", C + "/{card_id}", path_params={"card_id": cid})
    assert st == 200 and r["card"]["id"] == cid
    st, r = _call(mcp, "GET", C + "/{card_id}", path_params={"card_id": "Fmissing"})
    assert st == 404
    print("PASS card get + 404")

    # --- edit in place (no new timepoint) ---
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"title": "香港迪士尼乐园"})
    assert st == 200 and r["card"]["current"]["title"] == "香港迪士尼乐园"
    assert len(r["card"]["history"]) == 1  # still one revision
    print("PASS edit in place")

    # --- new revision (latest wins) ---
    st, r = _call(mcp, "POST", C + "/{card_id}/revisions", path_params={"card_id": cid},
                  body={"content": "回家", "valid_at": "2026-03-05"})
    assert st == 200 and r["card"]["current"]["content"] == "回家"
    assert r["card"]["current"]["title"] == "香港迪士尼乐园"  # title carried
    st, r = _call(mcp, "GET", C + "/{card_id}/revisions", path_params={"card_id": cid})
    assert len(r["revisions"]) == 2
    print("PASS new revision + list")

    # --- membership: card is in hk + fav ---
    st, r = _call(mcp, "GET", C + "/{card_id}/folders", path_params={"card_id": cid})
    assert {f["id"] for f in r["folders"]} == {hk, fav}
    assert [f["id"] for f in r["favorites"]] == [fav]
    # add to travel, then remove
    st, r = _call(mcp, "POST", C + "/{card_id}/folders", path_params={"card_id": cid},
                  body={"folder_id": travel})
    assert st == 200 and r["status"] == "linked"
    st, r = _call(mcp, "POST", C + "/{card_id}/folders", path_params={"card_id": cid},
                  body={"folder_id": travel})
    assert r["status"] == "already_linked"  # idempotent
    st, r = _call(mcp, "DELETE", C + "/{card_id}/folders/{folder_id}",
                  path_params={"card_id": cid, "folder_id": travel})
    assert st == 200 and r["status"] == "unlinked"
    print("PASS membership add/remove/idempotent")

    # --- second card + folder timeline (recursive by default) ---
    _call(mcp, "POST", C, body={"title": "成都", "content": "火锅",
                                "valid_at": "2026-06-10", "folder_ids": [travel]})
    st, r = _call(mcp, "GET", F + "/{folder_id}/timeline", path_params={"folder_id": travel})
    titles = [e["title"] for e in r["timeline"]]
    assert titles == ["香港迪士尼乐园", "成都"], titles  # sorted by date, hk (03-05) before chengdu (06-10)
    # non-recursive: only directly-filed card in travel
    st, r = _call(mcp, "GET", F + "/{folder_id}/cards", path_params={"folder_id": travel},
                  query={"recursive": "0"})
    assert {c["current"]["title"] for c in r["cards"]} == {"成都"}
    print("PASS folder timeline + recursive scoping")

    # --- delete folder re-parents child, keeps cards ---
    st, r = _call(mcp, "DELETE", F + "/{folder_id}", path_params={"folder_id": travel})
    assert st == 200
    st, r = _call(mcp, "GET", C + "/{card_id}", path_params={"card_id": cid})
    assert st == 200  # card survived folder deletion
    print("PASS delete folder keeps cards")

    # --- delete card ---
    st, r = _call(mcp, "DELETE", C + "/{card_id}", path_params={"card_id": cid})
    assert st == 200
    st, r = _call(mcp, "DELETE", C + "/{card_id}", path_params={"card_id": cid})
    assert st == 404  # already gone
    print("PASS delete card + 404")

    print(f"\nAll cards_api endpoint tests passed. ({len(mcp.routes)} routes registered)")


if __name__ == "__main__":
    main()
