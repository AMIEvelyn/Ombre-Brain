"""Tests for cards_mcp.register_card_tools (the MCP tools Lin Zhan calls).

Captures the registered tool functions via a fake mcp and drives them against a
real CardStore. Run: `python3 tests/test_cards_mcp.py`.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards_store import CardStore  # noqa: E402
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


def _fake_read_attachment_text(attachment):
    """Stand-in for server.py's _facts_attachment_text: 'reads' whatever text
    was stashed in the fake attachment dict's own "_text" field, mirroring
    the real function's None-for-unreadable-formats contract."""
    return attachment.get("_text")


FAKE_BUCKETS = {
    "b1": {"name": "香港迪士尼一日游", "date": "2026-03-02", "content_preview": "玩了城堡烟花秀"},
}


async def _fake_bucket_summary(bucket_id):
    """Stand-in for server.py's _bucket_link_summary. None = bucket no longer
    exists, matching the real contract."""
    return FAKE_BUCKETS.get(bucket_id)


class _FakeImage:
    """Stand-in for mcp.server.fastmcp.Image -- card_attachment_view just
    needs to pass through whatever read_attachment_image returns, so a
    sentinel object is enough; no need to exercise the real Image class
    here (that's an integration concern, verified on real deployment)."""
    def __init__(self, path):
        self.path = path


def _fake_read_attachment_image(attachment):
    ext = os.path.splitext(str(attachment.get("url") or ""))[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return None
    if attachment.get("_missing"):
        return None
    return _FakeImage(attachment.get("url"))


def _fake_read_attachment_full_text(attachment):
    """Stand-in for server.py's _facts_attachment_full_text: untruncated,
    stashed on the fake attachment dict's own "_full_text" field."""
    return attachment.get("_full_text")


def _fake_attachment_outline(attachment):
    """Stand-in for server.py's _facts_attachment_outline: whatever section
    list was stashed on the fake attachment dict's own "_outline" field.
    None = format not outline-able; [] = outline-able but nothing detected."""
    return attachment.get("_outline")


def main():
    tmp = tempfile.mkdtemp()
    store = CardStore(db_path=os.path.join(tmp, "cards.sqlite"))
    mcp = FakeMCP()
    cards_mcp.register_card_tools(
        mcp, store,
        read_attachment_text=_fake_read_attachment_text,
        read_attachment_full_text=_fake_read_attachment_full_text,
        read_attachment_image=_fake_read_attachment_image,
        attachment_outline=_fake_attachment_outline,
        bucket_summary=_fake_bucket_summary,
    )
    assert set(mcp.tools) == {
        "card_lookup", "folder_timeline", "card_history", "card_attachment_read",
        "card_attachment_outline", "card_attachment_view", "card_buckets",
    }
    card_lookup = mcp.tools["card_lookup"]
    folder_timeline = mcp.tools["folder_timeline"]
    card_history = mcp.tools["card_history"]
    card_attachment_outline = mcp.tools["card_attachment_outline"]
    card_attachment_read = mcp.tools["card_attachment_read"]
    card_attachment_view = mcp.tools["card_attachment_view"]
    card_buckets = mcp.tools["card_buckets"]

    # --- store helpers used by the tools ---
    assert store.search_cards("x") == []
    yilan = store.create_folder("一澜")
    food = store.create_folder("喜欢的食物", parent_id=yilan)
    fav = store.create_folder("林湛收藏", parent_id=yilan, is_favorite=True)
    assert store.folder_path(food) == "一澜 / 喜欢的食物"
    print("PASS store helpers (path)")

    suanla = store.create_card(title="酸辣粉", content="爱吃", valid_at="2026-01-01",
                               folder_ids=[food, fav])
    store.add_revision(suanla, content="不爱吃了", valid_at="2026-05-01")
    store.create_card(title="冰粉", content="夏天最爱", valid_at="2026-02-01", folder_ids=[food])

    # search: current content is searchable ("不爱吃了" is now current)
    assert {c["current"]["title"] for c in store.search_cards("不爱吃")} == {"酸辣粉"}
    assert {c["current"]["title"] for c in store.search_cards("", folder_id=food)} == {"酸辣粉", "冰粉"}
    print("PASS search_cards")

    # --- card_lookup output: leads with CURRENT state, hints history, shows star ---
    out = _run(card_lookup(query="酸辣粉"))
    assert "【酸辣粉】" in out
    assert "现在：一澜：不爱吃了" in out       # current = latest revision, not "爱吃"; labeled (2026-07-17)
    assert "有 2 条历史" in out               # history folded behind a hint
    assert "⭐" in out and "林湛收藏" in out   # favorite membership surfaced
    print("PASS card_lookup current-state + history hint + star")

    # empty query lists all; folder scoping works
    out_food = _run(card_lookup(folder="喜欢的食物"))
    assert "酸辣粉" in out_food and "冰粉" in out_food
    # miss
    assert "没搜到" in _run(card_lookup(query="不存在的东西"))
    print("PASS card_lookup scope + miss")

    # --- folder_timeline: narrative ribbon sorted by date ---
    travel = store.create_folder("旅行", parent_id=yilan)
    hk = store.create_folder("香港", parent_id=travel)
    a = store.create_card(title="香港", content="出发", valid_at="2026-03-01", folder_ids=[hk])
    store.add_revision(a, content="回家", valid_at="2026-03-05")
    store.create_card(title="成都", content="火锅", valid_at="2026-06-10", folder_ids=[travel])
    tl = _run(folder_timeline(folder="旅行"))
    assert "时间线" in tl
    i_hk, i_cd = tl.index("香港"), tl.index("成都")
    assert i_hk < i_cd                        # sorted ascending by date
    assert "2026-03-05" in tl and "回家" in tl  # card sits at its CURRENT date
    assert "（2 条历史）" in tl
    # non-recursive excludes the subfolder card
    tl_flat = _run(folder_timeline(folder="旅行", recursive=False))
    assert "成都" in tl_flat and "香港" not in tl_flat
    print("PASS folder_timeline ribbon + recursive")

    # --- ambiguous / missing folder resolution ---
    store.create_folder("喜欢的食物", parent_id=store.create_folder("林湛"))  # a second "喜欢的食物"
    assert "多个文件夹匹配" in _run(folder_timeline(folder="喜欢的食物"))
    assert "没找到" in _run(folder_timeline(folder="根本没有这个"))
    print("PASS folder resolution (ambiguous + missing)")

    # --- card_history: the other half of card_lookup's "有 N 条历史" hint ---
    hist = _run(card_history(card="酸辣粉"))
    assert "完整时间线（共 2 条" in hist
    assert hist.index("不爱吃了") < hist.index("爱吃")   # newest first
    assert "（当前）" in hist
    assert "2026-05-01" in hist and "2026-01-01" in hist
    print("PASS card_history full timeline, newest first")

    assert _run(card_history(card=suanla)) == hist        # resolves by id too
    print("PASS card_history resolve by id")

    store.create_card(title="酸辣粉", content="第二张同名卡", valid_at="2026-04-01")
    assert "多张卡匹配" in _run(card_history(card="酸辣粉"))
    assert "没找到" in _run(card_history(card="根本没有这张卡"))
    print("PASS card_history ambiguous + missing")

    # --- card_attachment_read: text attachments readable, images/pdf are not ---
    doc = store.create_card(
        title="旅行攻略",
        content="香港三天两夜",
        attachments=[
            {"type": "image", "label": "封面图.png", "url": "/api/facts-skeleton/attachments/a.png"},
            {"type": "file", "label": "行程.md", "url": "/api/facts-skeleton/attachments/b.md",
             "_text": "第一天：迪士尼\n第二天：太平山"},
            {"type": "file", "label": "船票.doc", "url": "/api/facts-skeleton/attachments/c.doc",
             "_text": None},  # legacy binary .doc -- still genuinely unsupported, unlike .pdf/.docx now
        ],
    )
    out = _run(card_attachment_read(card="旅行攻略"))
    assert "封面图.png" in out and "读不了图片内容" in out
    assert "行程.md" in out and "第一天：迪士尼" in out
    assert "船票.doc" in out and "没有可用的解析库" in out
    print("PASS card_attachment_read: image/text/unsupported all handled")

    only_doc = _run(card_attachment_read(card="旅行攻略", label="行程"))
    assert "行程.md" in only_doc and "封面图.png" not in only_doc
    print("PASS card_attachment_read label filter")

    no_atts = store.create_card(title="没有附件的卡", content="x")
    assert "没有附件" in _run(card_attachment_read(card=no_atts))
    print("PASS card_attachment_read: card with no attachments")

    # --- card_attachment_outline + card_attachment_read pagination (慁-style long doc) ---
    ch1, ch2, ch3 = "第一章 开始", "第二章 发展", "第三章 结局"
    body1, body2, body3 = "A" * 50, "B" * 50, "C" * 50
    novel_text = f"{ch1}\n{body1}\n{ch2}\n{body2}\n{ch3}\n{body3}"
    novel_outline = [
        {"section_id": "s0", "title": ch1, "level": 1, "start_position": novel_text.index(ch1)},
        {"section_id": "s1", "title": ch2, "level": 1, "start_position": novel_text.index(ch2)},
        {"section_id": "s2", "title": ch3, "level": 1, "start_position": novel_text.index(ch3)},
    ]
    novel = store.create_card(
        title="慁",
        attachments=[{
            "type": "file", "label": "慁.docx", "url": "/api/facts-skeleton/attachments/novel.docx",
            "_full_text": novel_text, "_outline": novel_outline,
        }],
    )

    outline_out = _run(card_attachment_outline(card="慁"))
    assert "章节结构（共 3 节）" in outline_out
    assert "[s0] 第一章 开始" in outline_out and "[s2] 第三章 结局" in outline_out
    print("PASS card_attachment_outline: real chapter structure")

    ch2_out = _run(card_attachment_read(card="慁", section_id="s1"))
    assert "第二章 发展" in ch2_out
    assert body2 in ch2_out and body1 not in ch2_out and body3 not in ch2_out  # only that chapter's text
    print("PASS card_attachment_read section_id: exact chapter slice, not neighbors")

    assert "没找到 section_id" in _run(card_attachment_read(card="慁", section_id="s99"))
    print("PASS card_attachment_read section_id: unknown id handled")

    page1 = _run(card_attachment_read(card="慁", start=0, max_chars=10))
    assert "还有更多内容" in page1 and "start=10" in page1
    page2 = _run(card_attachment_read(card="慁", start=10, max_chars=10))
    assert page1 != page2
    tail = _run(card_attachment_read(card="慁", start=len(novel_text) - 5, max_chars=100))
    assert "还有更多内容" not in tail  # reached the end, no next-page prompt
    print("PASS card_attachment_read start/max_chars: paging + has_more/next_start signal")

    # no chapter structure detected -- an honest [] result, not an error
    no_structure = store.create_card(
        title="随笔",
        attachments=[{
            "type": "file", "label": "随笔.md", "url": "/api/facts-skeleton/attachments/notes.md",
            "_full_text": "今天天气不错，写了点感想，没分章节。", "_outline": [],
        }],
    )
    assert "没有检测到章节结构" in _run(card_attachment_outline(card="随笔"))
    print("PASS card_attachment_outline: honestly reports no structure detected")

    # ambiguous attachment: pagination/outline need exactly one, won't guess
    two_files = store.create_card(
        title="双文件卡",
        attachments=[
            {"type": "file", "label": "a.md", "url": "/x/a.md", "_full_text": "AAA", "_outline": []},
            {"type": "file", "label": "b.md", "url": "/x/b.md", "_full_text": "BBB", "_outline": []},
        ],
    )
    assert "有多个附件匹配" in _run(card_attachment_outline(card="双文件卡"))
    assert "有多个附件匹配" in _run(card_attachment_read(card="双文件卡", start=0, max_chars=10))
    only_a = _run(card_attachment_outline(card="双文件卡", label="a.md"))
    assert "a.md" in only_a
    print("PASS card_attachment_outline/read: ambiguous attachment requires label, doesn't guess")

    # attachment_outline=None (not wired up)
    mcp_no_outline = FakeMCP()
    cards_mcp.register_card_tools(
        mcp_no_outline, store,
        read_attachment_text=_fake_read_attachment_text,
        read_attachment_full_text=_fake_read_attachment_full_text,
    )
    assert "还没接上章节解析" in _run(mcp_no_outline.tools["card_attachment_outline"](card="慁"))
    assert "还没接上章节解析" in _run(mcp_no_outline.tools["card_attachment_read"](card="慁", section_id="s0"))
    print("PASS card_attachment_outline/read: disabled cleanly when attachment_outline not wired up")

    # read_attachment_full_text=None (pagination disabled, whole-file read still fine)
    mcp_no_fulltext = FakeMCP()
    cards_mcp.register_card_tools(mcp_no_fulltext, store, read_attachment_text=_fake_read_attachment_text)
    assert "还没接上分段读取" in _run(mcp_no_fulltext.tools["card_attachment_read"](card="慁", start=0, max_chars=10))
    print("PASS card_attachment_read: pagination disabled cleanly when read_attachment_full_text not wired up")

    # read_attachment_text=None but read_attachment_full_text/attachment_outline ARE wired --
    # pagination must not require read_attachment_text at all (real bug caught by a deeper
    # real-server.py-style integration check, 2026-07-16: the function used to gate on
    # read_attachment_text before even checking whether this was the paginated branch).
    mcp_pagination_only = FakeMCP()
    cards_mcp.register_card_tools(
        mcp_pagination_only, store,
        read_attachment_full_text=_fake_read_attachment_full_text,
        attachment_outline=_fake_attachment_outline,
    )
    para_only_read = mcp_pagination_only.tools["card_attachment_read"]
    para_only_outline = mcp_pagination_only.tools["card_attachment_outline"]
    assert "第二章 发展" in _run(para_only_read(card="慁", section_id="s1"))
    assert "章节结构（共 3 节）" in _run(para_only_outline(card="慁"))
    assert "还没接上附件内容读取" in _run(para_only_read(card="慁"))  # whole-file mode still needs it
    print("PASS card_attachment_read: section_id/start pagination works without read_attachment_text configured")

    # --- read_attachment_text=None (not wired up on this deployment) ---
    mcp_unwired = FakeMCP()
    cards_mcp.register_card_tools(mcp_unwired, store)
    disabled = _run(mcp_unwired.tools["card_attachment_read"](card="旅行攻略"))
    assert "还没接上附件内容读取" in disabled
    print("PASS card_attachment_read: disabled cleanly when not wired up")

    # --- card_attachment_view: experimental image-sending tool ---
    view = _run(card_attachment_view(card="旅行攻略"))
    assert isinstance(view, _FakeImage) and view.path == "/api/facts-skeleton/attachments/a.png"
    print("PASS card_attachment_view: returns the image object directly")

    only_image_by_label = _run(card_attachment_view(card="旅行攻略", label="封面"))
    assert isinstance(only_image_by_label, _FakeImage)
    print("PASS card_attachment_view: label filter")

    two_images_card = store.create_card(
        title="双图卡",
        attachments=[
            {"type": "image", "label": "a.png", "url": "/x/a.png"},
            {"type": "image", "label": "b.png", "url": "/x/b.png"},
        ],
    )
    ambiguous = _run(card_attachment_view(card="双图卡"))
    assert isinstance(ambiguous, str) and "有多张图片匹配" in ambiguous
    print("PASS card_attachment_view: ambiguous (multiple images, no label)")

    assert "没找到匹配的图片附件" in _run(card_attachment_view(card="没有附件的卡"))
    print("PASS card_attachment_view: no image attachments")

    disabled_view = _run(mcp_unwired.tools["card_attachment_view"](card="旅行攻略"))
    assert isinstance(disabled_view, str) and "还没接上图片查看" in disabled_view
    print("PASS card_attachment_view: disabled cleanly when not wired up")

    # --- at=: reading a merged-away card's attachments off its own (older) timepoint ---
    # 2026-07-17: real bug Yi Lan + Lin Zhan hit testing a real merge -- merge
    # never touches a revision's attachments, but every attachment tool used
    # to only ever look at "current", so the losing card's attachments became
    # unreachable. `at` (a YYYY-MM-DD date, same as card_history shows) fixes it.
    merge_a = store.create_card(
        title="testA", content="A", valid_at="2026-01-01",
        attachments=[
            {"type": "file", "label": "testA.md", "url": "/x/testA.md", "_text": "A的内容"},
            {"type": "image", "label": "snowA.png", "url": "/x/snowA.png"},
        ],
    )
    merge_b = store.create_card(
        title="testB", content="B", valid_at="2026-02-01",
        attachments=[{"type": "file", "label": "testB.md", "url": "/x/testB.md", "_text": "B的内容"}],
    )
    store.merge_cards(merge_b, merge_a)  # keep B (later date), discard A
    at_date = "2026-01-01"
    no_at = _run(card_attachment_read(card=merge_b))
    assert "testA.md" not in no_at and "B的内容" in no_at  # default (no at) is still just "current" = B
    with_at = _run(card_attachment_read(card=merge_b, at=at_date))
    assert "A的内容" in with_at and "testB.md" not in with_at  # at= reaches A's own timepoint instead
    assert isinstance(_run(card_attachment_view(card=merge_b, at=at_date)), _FakeImage)  # A's image, via at=
    bad_date = _run(card_attachment_read(card=merge_b, at="1999-01-01"))
    assert "没找到日期是" in bad_date
    print("PASS card_attachment_read/view: at= reaches a merged-away card's attachments by date")

    # --- card_lookup now hints at attachments + linked buckets (used to say nothing) ---
    out_doc = _run(card_lookup(query="旅行攻略"))
    assert "📎 附件：1 张图片，2 个文件（行程.md、船票.doc）" in out_doc
    print("PASS card_lookup hints attachment counts + file names")

    # --- card_buckets: the other half of card_lookup's "关联了 N 个记忆桶" hint ---
    assert "还没有关联任何记忆桶" in _run(card_buckets(card="旅行攻略"))
    store.add_bucket_link(doc, "b1")
    store.add_bucket_link(doc, "b_gone")  # a bucket that no longer exists
    out_doc2 = _run(card_lookup(query="旅行攻略"))
    assert "🫙 关联了 2 个记忆桶" in out_doc2
    bkts = _run(card_buckets(card="旅行攻略"))
    assert "关联的记忆桶（共 2 个）" in bkts
    assert "[evidence] 2026-03-02 【香港迪士尼一日游】(id: b1)：玩了城堡烟花秀" in bkts
    assert "b_gone（这个桶已经不存在了）" in bkts
    print("PASS card_buckets: real + vanished bucket, card_lookup count hint")

    # --- bucket_summary=None (not wired up on this deployment) ---
    disabled_buckets = _run(mcp_unwired.tools["card_buckets"](card="旅行攻略"))
    assert "还没接上记忆桶查询" in disabled_buckets
    print("PASS card_buckets: disabled cleanly when not wired up")

    print("\nAll cards_mcp tool tests passed.")


def real_fastmcp_registration_smoke_test():
    """FakeMCP above only stores functions in a dict -- it can't catch
    FastMCP's own schema generation blowing up at registration time, which
    is exactly what happened in production 2026-07-16: a `-> Union[str,
    Image]` return annotation on card_attachment_view crashed pydantic
    schema generation and took the whole server down on restart (FakeMCP-
    based tests all still passed). This registers against a real FastMCP
    instance and lists the tools, which is what actually caught it. Keep
    this whenever a tool's return type touches anything beyond plain
    str/bool/int/float/dict-of-those."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test_cards_mcp_smoke")
    tmp = tempfile.mkdtemp()
    store = CardStore(db_path=os.path.join(tmp, "cards.sqlite"))
    cards_mcp.register_card_tools(mcp, store)
    tools = _run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "card_lookup", "folder_timeline", "card_history", "card_attachment_read",
        "card_attachment_outline", "card_attachment_view", "card_buckets",
    }, names
    print(f"PASS real FastMCP registration smoke test ({len(tools)} tools, no schema errors)")


if __name__ == "__main__":
    main()
    real_fastmcp_registration_smoke_test()
