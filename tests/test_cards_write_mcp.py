"""Tests for cards_mcp.register_card_write_tools (Lin Zhan's write access).

Same FakeMCP-capture pattern as test_cards_mcp.py, driven against a real
CardStore. Run: `python3 tests/test_cards_write_mcp.py`.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards_store import CardStore, AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN  # noqa: E402
import cards_mcp  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _store():
    tmp = tempfile.mkdtemp()
    return CardStore(db_path=os.path.join(tmp, "cards.sqlite"))


def main():
    store = _store()
    mcp = FakeMCP()
    cards_mcp.register_card_write_tools(mcp, store)
    assert set(mcp.tools) == {
        "card_create", "card_create_folder", "card_add_to_folder", "card_remove_from_folder",
        "card_edit_title", "card_edit_tags", "card_add_content", "card_edit_content",
        "card_delete_content", "card_new_revision", "card_link_bucket", "card_unlink_bucket",
    }
    assert "card_delete" not in mcp.tools  # deliberately scoped out, see docstring
    card_create = mcp.tools["card_create"]
    card_create_folder = mcp.tools["card_create_folder"]
    card_add_to_folder = mcp.tools["card_add_to_folder"]
    card_remove_from_folder = mcp.tools["card_remove_from_folder"]
    card_edit_title = mcp.tools["card_edit_title"]
    card_edit_tags = mcp.tools["card_edit_tags"]
    card_add_content = mcp.tools["card_add_content"]
    card_edit_content = mcp.tools["card_edit_content"]
    card_delete_content = mcp.tools["card_delete_content"]
    card_new_revision = mcp.tools["card_new_revision"]
    card_link_bucket = mcp.tools["card_link_bucket"]
    card_unlink_bucket = mcp.tools["card_unlink_bucket"]

    # --- card_create: full parity fields, tagged to Lin Zhan ---
    out = _run(card_create(title="", content=""))
    assert "至少要给一个" in out
    print("PASS card_create: title+content both empty rejected")

    out = _run(card_create(title="ZhLanism 设定", content="世界观第一稿", tags=["设定"], valid_at="2026-06-01"))
    assert "已新建" in out and "ZhLanism 设定" in out
    cur = store.search_cards("ZhLanism")[0]["current"]
    assert cur["title_author"] == AUTHOR_LIN_ZHAN
    assert cur["content_segments"][0]["author"] == AUTHOR_LIN_ZHAN
    assert cur["tags"] == ["设定"]
    print("PASS card_create: fields set + tagged lin_zhan author")

    # --- card_create_folder + card_create with folder ---
    out = _run(card_create_folder(name="世界观", parent=""))
    assert "已新建文件夹" in out
    out2 = _run(card_create_folder(name="子设定", parent="世界观"))
    assert "世界观 / 子设定" in out2
    out3 = _run(card_create(title="放进文件夹的卡", folder="子设定"))
    assert "已新建" in out3
    card_in_folder = store.search_cards("放进文件夹的卡")[0]
    assert any(store.folder_path(f["id"]) == "世界观 / 子设定" for f in card_in_folder["folders"])
    print("PASS card_create_folder + card_create(folder=...) nesting works")

    out_bad_folder = _run(card_create(title="坏文件夹测试", folder="不存在的馆"))
    assert "没找到" in out_bad_folder
    print("PASS card_create: unknown folder name rejected, doesn't silently create one")

    # --- card_add_to_folder / card_remove_from_folder ---
    _run(card_create_folder(name="收藏夹测试"))
    add_out = _run(card_add_to_folder(card="放进文件夹的卡", folder="收藏夹测试"))
    assert "已加入" in add_out
    add_again = _run(card_add_to_folder(card="放进文件夹的卡", folder="收藏夹测试"))
    assert "已经在" in add_again
    rm_out = _run(card_remove_from_folder(card="放进文件夹的卡", folder="收藏夹测试"))
    assert "已移出" in rm_out
    print("PASS card_add_to_folder/card_remove_from_folder: idempotent add, real remove")

    # --- card_edit_title: no ownership check, tags title_author ---
    title_out = _run(card_edit_title(card="ZhLanism 设定", title="ZhLanism 设定（改）"))
    assert "标题已改成" in title_out
    cur = store.get_current_revision(store.search_cards("ZhLanism 设定（改）")[0]["id"])
    assert cur["title_author"] == AUTHOR_LIN_ZHAN
    assert "湛：" not in cur["title"] and "澜：" not in cur["title"]  # no text prefix, metadata only
    print("PASS card_edit_title: no confirmation needed, no text prefix baked into title")

    # --- card_edit_tags: whole-list replace ---
    tags_out = _run(card_edit_tags(card="ZhLanism 设定（改）", tags=["设定", "世界观"]))
    assert "设定" in tags_out and "世界观" in tags_out
    cur = store.get_current_revision(store.search_cards("ZhLanism 设定（改）")[0]["id"])
    assert cur["tags"] == ["设定", "世界观"]
    empty_out = _run(card_edit_tags(card="ZhLanism 设定（改）", tags=[]))
    assert "空" in empty_out
    print("PASS card_edit_tags: whole-list replace including clearing to empty")

    # --- card_add_content: always allowed, no force needed ---
    yl_card = store.create_card(title="沟通方式", content="我希望吵架后能先冷静半小时", author=AUTHOR_YI_LAN)
    add_content_out = _run(card_add_content(card="沟通方式", text="我会记得先深呼吸，不马上回复"))
    assert "已添加" in add_content_out
    cur = store.get_current_revision(yl_card)
    assert len(cur["content_segments"]) == 2
    assert [s["author"] for s in cur["content_segments"]] == [AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN]
    print("PASS card_add_content: appends without touching Yi Lan's segment, no force needed")

    # --- card_edit_content / card_delete_content: rule C in action ---
    yl_seg_id = cur["content_segments"][0]["id"]
    lz_seg_id = cur["content_segments"][1]["id"]

    blocked = _run(card_edit_content(card="沟通方式", segment_id=yl_seg_id, text="被改了"))
    assert "这段是一澜写的" in blocked and "我希望吵架后能先冷静半小时" in blocked
    unchanged = store.get_current_revision(yl_card)["content_segments"][0]["text"]
    assert unchanged == "我希望吵架后能先冷静半小时"  # unforced attempt didn't mutate
    print("PASS card_edit_content: touching Yi Lan's segment blocked with a real preview, not mutated")

    forced = _run(card_edit_content(card="沟通方式", segment_id=yl_seg_id, text="一澜的话被林湛改了", force=True))
    assert "已修改" in forced
    print("PASS card_edit_content: force=True actually applies the edit")

    own_edit = _run(card_edit_content(card="沟通方式", segment_id=lz_seg_id, text="林湛改了自己写的"))
    assert "已修改" in own_edit  # editing own segment never needs force
    print("PASS card_edit_content: editing your own segment never needs force")

    blocked_delete = _run(card_delete_content(card="沟通方式", segment_id=yl_seg_id))
    assert "这段是一澜写的" in blocked_delete
    assert len(store.get_current_revision(yl_card)["content_segments"]) == 2  # untouched
    forced_delete = _run(card_delete_content(card="沟通方式", segment_id=yl_seg_id, force=True))
    assert "已删除" in forced_delete
    assert len(store.get_current_revision(yl_card)["content_segments"]) == 1
    print("PASS card_delete_content: same rule-C gate as edit, force actually deletes")

    missing_seg = _run(card_edit_content(card="沟通方式", segment_id="seg_does_not_exist", text="x", force=True))
    assert "没找到" in missing_seg
    print("PASS card_edit_content: unknown segment_id handled cleanly")

    # --- card_new_revision: content REPLACES, rule-C gated (2026-07-17, second pass) ---
    novel_card = store.create_card(title="慁", content="第一版内容", author=AUTHOR_YI_LAN)
    blocked_rev = _run(card_new_revision(card="慁", content="林湛的全新第二版", title="慁（终稿）"))
    assert "这段是一澜写的" in blocked_rev and "第一版内容" in blocked_rev
    assert len(store.list_revisions(novel_card)) == 1  # unforced attempt didn't create a revision
    print("PASS card_new_revision: replacing content that would lose Yi Lan's segment is blocked with a preview")

    rev_out = _run(card_new_revision(card="慁", content="林湛的全新第二版", title="慁（终稿）", force=True))
    assert "已加新时间点" in rev_out
    history = store.list_revisions(novel_card)
    assert len(history) == 2
    assert history[0]["content"] == "林湛：林湛的全新第二版"  # new revision: replaced, not appended
    assert history[0]["content_segments"][0]["author"] == AUTHOR_LIN_ZHAN
    assert history[0]["title"] == "慁（终稿）" and history[0]["title_author"] == AUTHOR_LIN_ZHAN
    assert history[1]["content"] == "一澜：第一版内容"  # old revision untouched in history
    assert history[1]["content_segments"][0]["author"] == AUTHOR_YI_LAN
    print("PASS card_new_revision: force=True replaces (not merges) the new timepoint; old one intact in history")

    no_change_out = _run(card_new_revision(card="慁"))
    assert "已加新时间点" in no_change_out
    cur = store.get_current_revision(novel_card)
    assert cur["content"] == "林湛：林湛的全新第二版"  # carried forward unchanged when nothing passed
    print("PASS card_new_revision: omitted fields carry forward from the previous revision, no force needed")

    # --- card_link_bucket / card_unlink_bucket ---
    link_out = _run(card_link_bucket(card="慁", bucket_id="b_evidence_1"))
    assert "已关联" in link_out
    link_again = _run(card_link_bucket(card="慁", bucket_id="b_evidence_1"))
    assert "已经关联过了" in link_again
    unlink_out = _run(card_unlink_bucket(card="慁", bucket_id="b_evidence_1"))
    assert "已取消关联" in unlink_out
    unlink_again = _run(card_unlink_bucket(card="慁", bucket_id="b_evidence_1"))
    assert "本来就没关联" in unlink_again
    print("PASS card_link_bucket/card_unlink_bucket: idempotent link, real unlink")

    # --- missing-card resolution shared across all write tools ---
    for fn, kwargs in [
        (card_edit_title, {"card": "不存在的卡", "title": "x"}),
        (card_add_content, {"card": "不存在的卡", "text": "x"}),
        (card_new_revision, {"card": "不存在的卡"}),
        (card_link_bucket, {"card": "不存在的卡", "bucket_id": "b1"}),
    ]:
        out = _run(fn(**kwargs))
        assert "没找到" in out, (fn.__name__, out)
    print("PASS all write tools: unknown card handled via the same _resolve_card path")

    print("\nAll cards_write_mcp tool tests passed.")


def real_fastmcp_registration_smoke_test():
    """Same reasoning as test_cards_mcp.py's version -- FakeMCP can't catch
    FastMCP's own schema-generation failures (see the 2026-07-16 outage from
    a bad return-type annotation). Registers every read AND write tool
    together against a real FastMCP, exactly like server.py does."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test_cards_write_mcp_smoke")
    store = _store()
    cards_mcp.register_card_tools(mcp, store)
    cards_mcp.register_card_write_tools(mcp, store)
    tools = _run(mcp.list_tools())
    names = {t.name for t in tools}
    assert len(names) == 19, names
    assert "card_create" in names and "card_lookup" in names
    print(f"PASS real FastMCP registration smoke test ({len(tools)} read+write tools, no schema errors)")


if __name__ == "__main__":
    main()
    real_fastmcp_registration_smoke_test()
