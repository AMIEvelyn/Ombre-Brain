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

from cards_store import CardStore, AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN  # noqa: E402
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
    assert r["card"]["current"]["content"] == "一澜：出发"  # 2026-07-17: every segment gets labeled now
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
    assert st == 200 and r["card"]["current"]["content"] == "一澜：回家"
    assert r["card"]["current"]["title"] == "香港迪士尼乐园"  # title carried
    st, r = _call(mcp, "GET", C + "/{card_id}/revisions", path_params={"card_id": cid})
    assert len(r["revisions"]) == 2
    print("PASS new revision + list")

    # --- content_text (whole-content editing, 2026-07-17 second pass): add always
    # works, touching Lin Zhan's paragraph is gated by rule C -- no segment_id
    # anywhere in this flow (that concept was removed: Lin Zhan had no way to
    # discover one, making the old per-segment endpoints unusable in practice) ---
    shown = store.get_current_revision(cid)["content"]
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content_text": shown + "\n\n一澜补充的一段"})
    assert st == 200, r
    assert r["card"]["current"]["content_segments"][-1]["text"] == "一澜补充的一段"
    print("PASS content_text: adding a new paragraph always works, no auth conflict")

    # simulate Lin Zhan having added his own paragraph via his MCP tools (this
    # HTTP API is Dashboard-only/Yi-Lan-only, so injecting directly through
    # the store is the realistic way to set up a real cross-author card)
    store.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的段落")
    shown = store.get_current_revision(cid)["content"]

    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content_text": shown.replace("林湛写的段落", "被一澜改了")})
    assert st == 409, r
    assert r["error"] == "ownership_conflict" and r["author"] == AUTHOR_LIN_ZHAN and "林湛写的段落" in r["text_preview"]
    assert r["message"] == "你修改了林湛的内容，需要 force=true 才能保存。"  # 2026-07-17: less "overwrite"-sounding wording
    print("PASS content_text: touching Lin Zhan's paragraph returns structured 409, not a generic error")

    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content_text": shown.replace("林湛写的段落", "被一澜改了"), "force": True})
    assert st == 200, r
    print("PASS content_text: force=true actually applies it")

    shown = store.get_current_revision(cid)["content"]
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content_text": shown.replace("一澜补充的一段", "一澜改了自己写的")})
    assert st == 200, r  # own paragraph never needs force
    print("PASS content_text: editing your own paragraph never needs force")

    # dropping Lin Zhan's paragraph entirely is the same rule-C gate
    shown = store.get_current_revision(cid)["content"]
    dropped = "\n\n".join(p for p in shown.split("\n\n") if "被一澜改了" not in p)
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid}, body={"content_text": dropped})
    assert st == 409 and r["error"] == "ownership_conflict", r
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content_text": dropped, "force": True})
    assert st == 200, r
    print("PASS content_text: dropping Lin Zhan's paragraph needs force too, same as editing it")

    # --- whole-string edit_card(content=...) is rule-C gated too (it wipes every segment) ---
    store.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="又一段林湛写的")
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid}, body={"content": "整段重写"})
    assert st == 409 and r["error"] == "ownership_conflict", r
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": cid},
                  body={"content": "整段重写", "force": True})
    assert st == 200 and r["card"]["current"]["content"] == "一澜：整段重写", r
    print("PASS edit_card(content=...): same rule-C gate as delete, force overrides")

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

    # --- content editor modal flow: content_text is parsed server-side (2026-07-17) ---
    st, r = _call(mcp, "POST", C, body={"title": "编辑器测试", "content_text": "一澜：我写的\n\n没打标签的一句"})
    assert st == 200, r
    editor_cid = r["card"]["id"]
    assert r["card"]["current"]["content"] == "一澜：我写的\n\n没打标签的一句"
    assert [s["author"] for s in r["card"]["current"]["content_segments"]] == [AUTHOR_YI_LAN, None]
    print("PASS create_card: content_text parsed into segments (tagged + untagged)")

    # add Lin Zhan's segment directly (his MCP tools are the real path), then
    # edit via content_text preserving his paragraph -- no force needed
    store.add_content_segment(editor_cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    shown = store.get_current_revision(editor_cid)["content"]
    assert shown == "一澜：我写的\n\n没打标签的一句\n\n林湛：林湛写的"
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": editor_cid},
                  body={"content_text": shown.replace("我写的", "我改过的")})
    assert st == 200, r  # his paragraph's text is still present verbatim -> no conflict
    assert r["card"]["current"]["content"] == "一澜：我改过的\n\n没打标签的一句\n\n林湛：林湛写的"
    print("PASS edit_card: content_text editing only Yi Lan's own paragraph needs no force")

    # now drop his paragraph entirely via content_text -- needs force, same 409 shape
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": editor_cid},
                  body={"content_text": "一澜：我改过的\n\n没打标签的一句"})
    assert st == 409 and r["error"] == "ownership_conflict" and r["author"] == AUTHOR_LIN_ZHAN, r
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": editor_cid},
                  body={"content_text": "一澜：我改过的\n\n没打标签的一句", "force": True})
    assert st == 200 and "林湛" not in r["card"]["current"]["content"], r
    print("PASS edit_card: content_text dropping Lin Zhan's paragraph is rule-C gated, force overrides")

    # --- New Revision is now rule-C gated too (2026-07-17, second pass) ---
    store.add_content_segment(editor_cid, author=AUTHOR_LIN_ZHAN, text="林湛又写的")
    cur_content = store.get_current_revision(editor_cid)["content"]
    st, r = _call(mcp, "POST", C + "/{card_id}/revisions", path_params={"card_id": editor_cid},
                  body={"content": "整体换成新版本"})
    assert st == 409 and r["error"] == "ownership_conflict", r
    assert store.get_current_revision(editor_cid)["content"] == cur_content  # unforced attempt didn't create a revision
    st, r = _call(mcp, "POST", C + "/{card_id}/revisions", path_params={"card_id": editor_cid},
                  body={"content": "整体换成新版本", "force": True})
    assert st == 200 and r["card"]["current"]["content"] == "一澜：整体换成新版本", r
    print("PASS add_revision: losing Lin Zhan's segment needs force, same 409 shape as edit_card")

    # --- title bug fix: unchanged title text must not steal title_author (2026-07-17) ---
    store.set_title(editor_cid, "林湛起的标题", author=AUTHOR_LIN_ZHAN)
    assert store.get_current_revision(editor_cid)["title_author"] == AUTHOR_LIN_ZHAN
    # Dashboard's Edit form always sends the title field back, even unchanged --
    # this must not silently reassign it to Yi Lan just because she edited content
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": editor_cid},
                  body={"title": "林湛起的标题", "content": "一澜改的内容", "force": True})
    assert st == 200, r
    assert r["card"]["current"]["title_author"] == AUTHOR_LIN_ZHAN  # untouched, title text didn't change
    assert r["card"]["current"]["title"] == "林湛起的标题"
    # actually changing the title text DOES reassign it (Dashboard is Yi-Lan-only)
    st, r = _call(mcp, "PATCH", C + "/{card_id}", path_params={"card_id": editor_cid}, body={"title": "一澜改的标题"})
    assert st == 200 and r["card"]["current"]["title_author"] == AUTHOR_YI_LAN, r
    print("PASS edit_card: title_author only reassigned when the title text actually changes")

    # --- GET /cards?q= : card search, backs the Dashboard's merge-tool card picker ---
    st, r = _call(mcp, "POST", C, body={"title": "搜索测试卡", "content": "内容"})
    search_cid = r["card"]["id"]
    st, r = _call(mcp, "GET", C, query={})
    assert st == 200 and r["cards"] == [], r  # no q -- deliberately empty, not "everything"
    st, r = _call(mcp, "GET", C, query={"q": "搜索测试卡"})
    assert st == 200 and any(c["id"] == search_cid for c in r["cards"]), r
    print("PASS GET /cards?q=: empty query returns nothing, real query finds the card")

    # --- merge / dedup (§14 item 4) ---
    dup_a = store.create_card(title="体重记录A", content="120斤", tags=["健康"], valid_at="2026-01-01")
    dup_b = store.create_card(title="瘦了", content="115斤", tags=["减肥"], valid_at="2026-02-01")
    st, r = _call(mcp, "POST", C + "/merge/preview", body={"card_a": dup_a, "card_b": dup_b})
    assert st == 200 and r["preview"]["revision_count_after"] == 2, r
    st, r = _call(mcp, "POST", C + "/merge", body={"card_a": dup_a, "card_b": dup_b, "keep": dup_a})
    assert st == 200 and r["result"]["kept_id"] == dup_a and r["result"]["discarded_id"] == dup_b, r
    assert set(r["card"]["current"]["tags"]) == {"健康", "减肥"}
    st, r = _call(mcp, "GET", C + "/{card_id}", path_params={"card_id": dup_b})
    assert st == 200 and r["card"]["id"] == dup_a, r  # old id transparently redirects
    # bad requests
    st, r = _call(mcp, "POST", C + "/merge", body={"card_a": dup_a, "card_b": dup_b, "keep": "somewhere-else"})
    assert st == 400, r
    st, r = _call(mcp, "POST", C + "/merge/preview", body={"card_a": dup_a, "card_b": dup_a})
    assert st == 400, r  # can't merge a card with itself
    print("PASS merge/preview + merge: unions tags, redirects old id, rejects bad keep/self-merge")

    print(f"\nAll cards_api endpoint tests passed. ({len(mcp.routes)} routes registered)")


if __name__ == "__main__":
    main()
