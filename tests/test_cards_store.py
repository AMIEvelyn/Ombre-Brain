"""Isolated tests for cards_store.CardStore (facts-model v2 data layer).

Pure stdlib + sqlite -- no project deps, no network. Runs under pytest, or
standalone: `python3 tests/test_cards_store.py`.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards_store import (  # noqa: E402
    CardStore, AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN, SegmentOwnershipError,
)


def _store():
    tmp = tempfile.mkdtemp()
    return CardStore(db_path=os.path.join(tmp, "cards.sqlite"))


def test_create_card_and_current():
    s = _store()
    cid = s.create_card(title="身高", content="170cm", tags=["体征"], valid_at="2026-01-01")
    assert cid.startswith("F")
    cur = s.get_current_revision(cid)
    assert cur["title"] == "身高" and cur["content"] == "一澜：170cm"
    assert cur["tags"] == ["体征"]
    card = s.get_card(cid)
    assert card["current"]["content"] == "一澜：170cm"
    assert len(card["history"]) == 1


def test_new_revision_latest_wins_and_carries_title():
    s = _store()
    cid = s.create_card(title="身高", content="170cm", valid_at="2026-01-01")
    # New Revision only supplies content + a newer date; title carries over.
    s.add_revision(cid, content="175cm", valid_at="2026-06-01")
    cur = s.get_current_revision(cid)
    assert cur["content"] == "一澜：175cm"       # latest valid_at wins
    assert cur["title"] == "身高"           # title carried from previous revision
    hist = s.list_revisions(cid)
    assert len(hist) == 2
    assert [h["content"] for h in hist] == ["一澜：175cm", "一澜：170cm"]  # newest first


def test_backdated_revision_is_not_current():
    s = _store()
    cid = s.create_card(title="体重", content="100斤", valid_at="2026-06-01")
    s.add_revision(cid, content="98斤", valid_at="2026-03-01")  # older event date
    assert s.get_current_revision(cid)["content"] == "一澜：100斤"     # newest date still current


def test_edit_current_in_place_no_new_timepoint():
    s = _store()
    cid = s.create_card(title="酸辣粉", content="爱吃", valid_at="2026-01-01")
    ok = s.edit_current(cid, title="酸辣粉（重庆）", content="超爱吃")
    assert ok
    assert len(s.list_revisions(cid)) == 1            # still ONE revision
    cur = s.get_current_revision(cid)
    assert cur["title"] == "酸辣粉（重庆）" and cur["content"] == "一澜：超爱吃"


def test_rename_does_not_split_timeline():
    # The old title-as-identity bug: renaming must not orphan history.
    s = _store()
    cid = s.create_card(title="身高", content="170cm", valid_at="2026-01-01")
    s.add_revision(cid, content="175cm", valid_at="2026-06-01")
    s.edit_current(cid, title="个子")           # rename the current revision
    assert len(s.list_revisions(cid)) == 2       # both timepoints still on ONE card
    assert s.get_current_revision(cid)["title"] == "个子"


def test_delete_card_cascades():
    s = _store()
    fid = s.create_folder("食物")
    cid = s.create_card(title="火锅", folder_ids=[fid])
    s.add_revision(cid, content="爱")
    assert s.delete_card(cid)
    assert s.get_card(cid) is None
    assert s.list_revisions(cid) == []
    assert s.get_card_folders(cid) == []
    assert s.delete_card(cid) is False           # already gone


def test_folder_tree_and_descendants():
    s = _store()
    yilan = s.create_folder("一澜")               # subject (top-level)
    food = s.create_folder("喜欢的食物", parent_id=yilan)
    snack = s.create_folder("零食类", parent_id=food)
    assert s.get_folder(food)["parent_id"] == yilan
    desc = set(s.descendant_folder_ids(yilan))
    assert desc == {yilan, food, snack}
    assert set(s.descendant_folder_ids(yilan, include_self=False)) == {food, snack}
    assert [f["id"] for f in s.list_folders(parent_id="")] == [yilan]  # only subject at top


def test_delete_folder_reparents_children_and_unlinks_cards():
    s = _store()
    root = s.create_folder("一澜")
    mid = s.create_folder("身体状态", parent_id=root)
    leaf = s.create_folder("指标", parent_id=mid)
    cid = s.create_card(title="身高", folder_ids=[mid])
    assert s.delete_folder(mid)
    assert s.get_folder(leaf)["parent_id"] == root   # child re-parented up, not lost
    assert s.get_card(cid) is not None               # card itself survives
    assert s.get_card_folders(cid) == []             # but its link to mid is gone


def test_membership_idempotent_and_removable():
    s = _store()
    f1 = s.create_folder("收藏", is_favorite=True)
    f2 = s.create_folder("物品")
    cid = s.create_card(title="婚戒")
    assert s.add_card_to_folder(cid, f1) is True
    assert s.add_card_to_folder(cid, f1) is False     # idempotent, no dup
    s.add_card_to_folder(cid, f2)
    folders = s.get_card_folders(cid)
    assert {f["id"] for f in folders} == {f1, f2}
    favs = s.get_card_favorite_folders(cid)
    assert [f["id"] for f in favs] == [f1]            # only the favorite collection
    # remove from one; card stays, may become orphan
    assert s.remove_card_from_folder(cid, f2)
    assert {f["id"] for f in s.get_card_folders(cid)} == {f1}


def test_orphan_card_allowed():
    s = _store()
    f = s.create_folder("食物")
    cid = s.create_card(title="路边摊", folder_ids=[f])
    s.remove_card_from_folder(cid, f)
    assert s.get_card_folders(cid) == []              # zero folders is allowed
    assert s.get_card(cid) is not None                # card still exists (unfiled)


def test_card_in_multiple_folders_playlist_model():
    s = _store()
    ring_fav = s.create_folder("林湛收藏", is_favorite=True)
    our_things = s.create_folder("我们物品")
    cid = s.create_card(title="婚戒", folder_ids=[ring_fav, our_things])
    assert {f["id"] for f in s.get_card_folders(cid)} == {ring_fav, our_things}
    # same card body reachable from either folder
    assert cid in {c["id"] for c in s.list_cards_in_folder(ring_fav)}
    assert cid in {c["id"] for c in s.list_cards_in_folder(our_things)}


def test_list_cards_recursive():
    s = _store()
    root = s.create_folder("我们")
    travel = s.create_folder("旅行", parent_id=root)
    hk = s.create_folder("香港", parent_id=travel)
    c1 = s.create_card(title="香港迪士尼", folder_ids=[hk])
    c2 = s.create_card(title="成都火锅", folder_ids=[travel])
    direct = {c["id"] for c in s.list_cards_in_folder(travel, recursive=False)}
    assert direct == {c2}                              # only directly-filed
    deep = {c["id"] for c in s.list_cards_in_folder(travel, recursive=True)}
    assert deep == {c1, c2}                            # includes subfolder card


def test_folder_timeline_one_dot_per_card_sorted():
    s = _store()
    travel = s.create_folder("旅行")
    a = s.create_card(title="香港", content="出发", valid_at="2026-03-01", folder_ids=[travel])
    s.add_revision(a, content="回家", valid_at="2026-03-05")   # a's main date -> latest 03-05
    s.create_card(title="成都", content="到达", valid_at="2026-06-10", folder_ids=[travel])
    tl = s.folder_timeline(travel)
    assert [e["title"] for e in tl] == ["香港", "成都"]         # sorted by date ascending
    assert len(tl) == 2                                        # one entry per card, not per timepoint
    hk_entry = tl[0]
    assert hk_entry["date"] == "2026-03-05"                    # positioned at its current date
    assert hk_entry["content"] == "一澜：回家"
    assert hk_entry["revision_count"] == 2                     # but knows it has history


def test_single_author_content_still_gets_labeled():
    # 2026-07-17 (Yi Lan, second pass): labeling isn't about segment count --
    # every segment with a real author renders with its label, even when
    # it's the only segment on the card, so Lin Zhan can tell at a glance
    # who wrote something even when only one of them did.
    s = _store()
    cid = s.create_card(title="身高", content="170cm", author=AUTHOR_YI_LAN)
    cur = s.get_current_revision(cid)
    assert cur["content"] == "一澜：170cm"
    assert cur["title_author"] == AUTHOR_YI_LAN
    assert cur["content_segments"] == [
        {"id": cur["content_segments"][0]["id"], "author": AUTHOR_YI_LAN, "text": "170cm"}
    ]


def test_untagged_segment_renders_plain_and_is_unprotected():
    # parse_content_text produces author=None segments for text that wasn't
    # tagged -- these render as plain text (no label) and don't block a
    # future replace the way a real foreign segment would (there's no one
    # to ask permission from).
    s = _store()
    cid = s.create_card(title="卡", content_segments=[
        {"id": "seg1", "author": None, "text": "没标签的一段普通文字"},
    ])
    cur = s.get_current_revision(cid)
    assert cur["content"] == "没标签的一段普通文字"  # no label, no colon
    # replacing it entirely never needs force -- untagged text protects nothing
    assert s.edit_current(cid, content="全新内容", author=AUTHOR_YI_LAN)


def test_add_content_segment_never_needs_force():
    # Rule C (Yi Lan, 2026-07-17): freely *adding* never needs permission.
    s = _store()
    cid = s.create_card(title="沟通方式", content="我希望吵架后能先冷静半小时", author=AUTHOR_YI_LAN)
    s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="我会记得先深呼吸，不马上回复")
    cur = s.get_current_revision(cid)
    assert len(cur["content_segments"]) == 2
    assert [seg["author"] for seg in cur["content_segments"]] == [AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN]
    # two or more segments DO get labeled in the flattened text (the
    # convention Yi Lan was typing by hand, now generated instead)
    assert cur["content"] == "一澜：我希望吵架后能先冷静半小时\n\n林湛：我会记得先深呼吸，不马上回复"


def test_edit_content_segment_requires_force_for_someone_elses_words():
    s = _store()
    cid = s.create_card(title="沟通方式", content="一澜写的", author=AUTHOR_YI_LAN)
    seg_id = s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    try:
        s.edit_content_segment(cid, seg_id, text="被改了", author=AUTHOR_YI_LAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError as e:
        assert e.author == AUTHOR_LIN_ZHAN
        assert "林湛写的" in e.text_preview
    # unforced attempt must not have mutated anything
    assert s.get_current_revision(cid)["content_segments"][1]["text"] == "林湛写的"
    # forced: allowed, and the segment keeps its ORIGINAL author (editing
    # someone else's words with permission doesn't make them yours)
    assert s.edit_content_segment(cid, seg_id, text="一澜改的", author=AUTHOR_YI_LAN, force=True)
    edited = s.get_current_revision(cid)["content_segments"][1]
    assert edited["text"] == "一澜改的" and edited["author"] == AUTHOR_LIN_ZHAN
    # editing your OWN segment never needs force
    own_seg_id = s.get_current_revision(cid)["content_segments"][0]["id"]
    assert s.edit_content_segment(cid, own_seg_id, text="一澜改了自己写的", author=AUTHOR_YI_LAN)


def test_delete_content_segment_requires_force_for_someone_elses_words():
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    seg_id = s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    try:
        s.delete_content_segment(cid, seg_id, author=AUTHOR_YI_LAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError:
        pass
    assert len(s.get_current_revision(cid)["content_segments"]) == 2  # untouched
    assert s.delete_content_segment(cid, seg_id, author=AUTHOR_YI_LAN, force=True)
    assert len(s.get_current_revision(cid)["content_segments"]) == 1


def test_edit_current_whole_string_replace_checks_ownership_too():
    # edit_current(content=...) blows away *every* existing segment, so it's
    # the same rule-C check as deleting someone else's segment.
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    try:
        s.edit_current(cid, content="整段重写", author=AUTHOR_YI_LAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError:
        pass
    assert s.edit_current(cid, content="整段重写", author=AUTHOR_YI_LAN, force=True)
    cur = s.get_current_revision(cid)
    assert cur["content"] == "一澜：整段重写"
    assert cur["content_segments"] == [
        {"id": cur["content_segments"][0]["id"], "author": AUTHOR_YI_LAN, "text": "整段重写"}
    ]
    # single-author card: replacing your own sole segment never needs force
    assert s.edit_current(cid, content="一澜自己又改了一次", author=AUTHOR_YI_LAN)


def test_edit_current_content_segments_preserving_foreign_text_needs_no_force():
    # 2026-07-17, second pass: the ownership check is now about whether the
    # foreign segment's exact text survives, not "did content= get called at
    # all" -- so passing a full content_segments list that keeps Lin Zhan's
    # segment untouched and just adds Yi Lan's own new one never needs force,
    # even though it goes through the same whole-list-replace code path.
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    cur = s.get_current_revision(cid)
    new_segments = cur["content_segments"] + [{"id": "seg_new", "author": AUTHOR_YI_LAN, "text": "一澜又加的"}]
    assert s.edit_current(cid, content_segments=new_segments, author=AUTHOR_YI_LAN)  # no force needed
    cur2 = s.get_current_revision(cid)
    assert [s2["text"] for s2 in cur2["content_segments"]] == ["一澜写的", "林湛写的", "一澜又加的"]
    # now actually drop the foreign segment's text -- this DOES need force
    dropped = [s2 for s2 in cur2["content_segments"] if s2["author"] != AUTHOR_LIN_ZHAN]
    try:
        s.edit_current(cid, content_segments=dropped, author=AUTHOR_YI_LAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError as e:
        assert e.author == AUTHOR_LIN_ZHAN and "林湛写的" in e.text_preview
    assert s.edit_current(cid, content_segments=dropped, author=AUTHOR_YI_LAN, force=True)


def test_title_author_tracked_per_revision_no_text_prefix():
    # Yi Lan's spec: title itself stays clean text, no "湛："/"澜：" baked in --
    # authorship is metadata only, and each revision remembers its own.
    s = _store()
    cid = s.create_card(title="慁", content="...", author=AUTHOR_YI_LAN)
    assert s.get_current_revision(cid)["title_author"] == AUTHOR_YI_LAN
    s.add_revision(cid, title="慁（终稿）", author=AUTHOR_LIN_ZHAN)
    history = s.list_revisions(cid)
    assert history[0]["title"] == "慁（终稿）" and history[0]["title_author"] == AUTHOR_LIN_ZHAN
    assert history[1]["title"] == "慁" and history[1]["title_author"] == AUTHOR_YI_LAN
    assert "湛：" not in history[0]["title"] and "澜：" not in history[1]["title"]
    # a revision that DOESN'T change the title carries the old title_author forward
    # (force=True: this content= replace crosses authors too, unrelated to
    # what this test checks -- title_author)
    s.add_revision(cid, content="新内容", author=AUTHOR_LIN_ZHAN, force=True)
    assert s.get_current_revision(cid)["title_author"] == AUTHOR_LIN_ZHAN  # unchanged from previous rev


def test_title_author_not_reassigned_when_title_text_unchanged():
    # Real bug found 2026-07-17: the Dashboard's Edit form always sends the
    # title field back (pre-filled, even when Yi Lan only meant to touch
    # content), so title_author must only move to the caller when the title
    # TEXT actually changes -- passing the same string along for the ride
    # must not silently steal a title that was Lin Zhan's.
    s = _store()
    cid = s.create_card(title="慁", content="第一版", author=AUTHOR_LIN_ZHAN)
    assert s.get_current_revision(cid)["title_author"] == AUTHOR_LIN_ZHAN
    # edit_current: same title string, different author calling -- must NOT flip
    # (force=True: this content= replace also crosses authors, which is a
    # separate concern from what this test is checking -- title_author)
    s.edit_current(cid, title="慁", content="改了内容", author=AUTHOR_YI_LAN, force=True)
    cur = s.get_current_revision(cid)
    assert cur["title_author"] == AUTHOR_LIN_ZHAN  # untouched
    assert cur["content"] == "一澜：改了内容"
    # actually changing the title text DOES reassign it
    s.edit_current(cid, title="慁（改名）", author=AUTHOR_YI_LAN)
    assert s.get_current_revision(cid)["title_author"] == AUTHOR_YI_LAN
    # same story for add_revision (New Revision) -- force=True throughout
    # since these calls also replace content across authors and this test
    # only cares about the title_author side effect, not content ownership
    # (that's covered by test_new_revision_requires_force_to_lose_foreign_segment)
    cid2 = s.create_card(title="卡", content="v1", author=AUTHOR_LIN_ZHAN)
    s.add_revision(cid2, title="卡", content="v2", author=AUTHOR_YI_LAN, force=True)
    assert s.get_current_revision(cid2)["title_author"] == AUTHOR_LIN_ZHAN  # unchanged text, unchanged author
    s.add_revision(cid2, title="卡（新）", content="v3", author=AUTHOR_YI_LAN, force=True)
    assert s.get_current_revision(cid2)["title_author"] == AUTHOR_YI_LAN  # text changed, author moves


def test_add_revision_content_replaces_segments_carry_forward_when_omitted():
    s = _store()
    cid = s.create_card(title="卡", content="第一版", author=AUTHOR_YI_LAN)
    # New Revision with no content specified carries the segments forward untouched
    s.add_revision(cid, title="改了标题", author=AUTHOR_LIN_ZHAN)
    cur = s.get_current_revision(cid)
    assert cur["content"] == "一澜：第一版"
    assert cur["content_segments"][0]["author"] == AUTHOR_YI_LAN
    # New Revision WITH content that would lose Yi Lan's segment needs force
    # now (2026-07-17, second pass -- New Revision is rule-C gated like Edit)
    try:
        s.add_revision(cid, content="第二版全新内容", author=AUTHOR_LIN_ZHAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError as e:
        assert e.author == AUTHOR_YI_LAN and "第一版" in e.text_preview
    s.add_revision(cid, content="第二版全新内容", author=AUTHOR_LIN_ZHAN, force=True)
    cur2 = s.get_current_revision(cid)
    assert cur2["content"] == "林湛：第二版全新内容"
    assert cur2["content_segments"][0]["author"] == AUTHOR_LIN_ZHAN
    history = s.list_revisions(cid)
    assert history[-1]["content_segments"][0]["author"] == AUTHOR_YI_LAN  # original untouched in history


def test_new_revision_requires_force_to_lose_foreign_segment():
    # 2026-07-17, second pass (Yi Lan's updated call): New Revision used to
    # replace content freely with no confirmation, on the theory that the
    # old revision stays visible in history either way. Now that the content
    # editor makes it obvious whose text is where, New Revision is gated the
    # same as Edit -- adding your own new revision content that doesn't
    # touch the other author's segment never needs force; losing it does.
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    current_segments = s.get_current_revision(cid)["content_segments"]
    # adding a new revision that keeps both existing segments and adds a
    # third (via content_segments) never needs force
    kept_plus_new = current_segments + [{"id": "seg_new", "author": AUTHOR_YI_LAN, "text": "一澜新加的"}]
    s.add_revision(cid, content_segments=kept_plus_new, author=AUTHOR_YI_LAN)
    assert [seg["text"] for seg in s.get_current_revision(cid)["content_segments"]] == ["一澜写的", "林湛写的", "一澜新加的"]


def test_add_revision_explicit_content_segments_full_control():
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    prior = s.get_current_revision(cid)["content_segments"]
    s.add_revision(
        cid,
        content_segments=prior + [{"id": "seg_new", "author": AUTHOR_LIN_ZHAN, "text": "林湛补充的"}],
    )
    cur = s.get_current_revision(cid)
    assert [seg["author"] for seg in cur["content_segments"]] == [AUTHOR_YI_LAN, AUTHOR_LIN_ZHAN]
    assert "一澜：一澜写的" in cur["content"] and "林湛：林湛补充的" in cur["content"]


def test_invalid_author_rejected():
    s = _store()
    try:
        s.create_card(title="x", content="y", author="someone_else")
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_migration_backfills_preexisting_rows_as_yi_lan():
    # Simulates a real pre-existing production DB (old schema, no
    # title_author/content_segments columns) and confirms opening it with
    # the new code migrates every existing row to yi_lan authorship, exactly
    # as Yi Lan asked: nothing she already wrote should show up unlabeled.
    import sqlite3
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "cards.sqlite")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE cards (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE card_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]', attachments TEXT NOT NULL DEFAULT '[]',
            valid_at TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.execute("INSERT INTO cards (id, created_at) VALUES ('F_old', '2026-01-01')")
    conn.execute(
        "INSERT INTO card_revisions (card_id, title, content, valid_at, created_at) VALUES (?, ?, ?, ?, ?)",
        ("F_old", "老卡片", "一澜很久以前写的", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    store = CardStore(db_path=db_path)  # opens the OLD db -- must migrate on init
    card = store.get_card("F_old")
    assert card["current"]["title_author"] == AUTHOR_YI_LAN
    # 2026-07-17, second pass: the flat `content` column is resynced from
    # content_segments on every startup, so a migrated old card gets the
    # same "一澜：" label a freshly-created single-segment card would.
    assert card["current"]["content"] == "一澜：一澜很久以前写的"
    assert card["current"]["content_segments"] == [
        {"id": card["current"]["content_segments"][0]["id"], "author": AUTHOR_YI_LAN, "text": "一澜很久以前写的"}
    ]
    # re-opening (simulating a second restart) must not double-wrap
    store2 = CardStore(db_path=db_path)
    assert len(store2.get_card("F_old")["current"]["content_segments"]) == 1


def test_parse_content_text_round_trips_with_render_content():
    s = _store()
    original = "一澜：第一段\n\n没打标签的一段\n\n林湛：第三段"
    parsed = s.parse_content_text(original)
    assert [(p["author"], p["text"]) for p in parsed] == [
        (AUTHOR_YI_LAN, "第一段"), (None, "没打标签的一段"), (AUTHOR_LIN_ZHAN, "第三段"),
    ]
    # feeding it straight back through _render_content reproduces the same text
    assert CardStore._render_content(parsed) == original


def test_parse_content_text_blank_paragraphs_dropped():
    s = _store()
    parsed = s.parse_content_text("一澜：有内容\n\n\n\n   \n\n林湛：也有内容")
    assert [p["text"] for p in parsed] == ["有内容", "也有内容"]


def test_content_editor_save_flow_via_parse_content_text():
    # This is the actual flow the Dashboard's content editor modal drives:
    # show the flattened text, let Yi Lan free-edit it (including inserting
    # her own tag via a button), parse back on save, force-gated the same
    # as any other whole-content replace.
    s = _store()
    cid = s.create_card(title="卡", content="一澜写的", author=AUTHOR_YI_LAN)
    s.add_content_segment(cid, author=AUTHOR_LIN_ZHAN, text="林湛写的")
    shown = s.get_current_revision(cid)["content"]
    assert shown == "一澜：一澜写的\n\n林湛：林湛写的"
    # she edits her own paragraph and adds a new untagged note, leaving his intact
    edited = "一澜：一澜改过的\n\n林湛：林湛写的\n\n随手写的一句备注"
    segments = s.parse_content_text(edited)
    assert s.edit_current(cid, content_segments=segments, author=AUTHOR_YI_LAN)  # no force needed
    cur = s.get_current_revision(cid)
    assert cur["content"] == edited
    # now she deletes his paragraph entirely -- this needs force
    edited_dropping_his = "一澜：一澜改过的\n\n随手写的一句备注"
    try:
        s.edit_current(cid, content_segments=s.parse_content_text(edited_dropping_his), author=AUTHOR_YI_LAN)
        assert False, "should have raised SegmentOwnershipError"
    except SegmentOwnershipError as e:
        assert e.author == AUTHOR_LIN_ZHAN and "林湛写的" in e.text_preview


def test_merge_interleaves_revisions_and_leaves_each_timepoint_untouched():
    s = _store()
    a = s.create_card(title="香港旅行A", content="出发", valid_at="2026-03-01")
    s.add_revision(a, content="到景点", valid_at="2026-03-03")
    b = s.create_card(title="香港旅行B", content="午饭", valid_at="2026-03-02")
    s.add_revision(b, content="回家", valid_at="2026-03-05")
    result = s.merge_cards(a, b)
    assert result["kept_id"] == a and result["discarded_id"] == b
    assert result["revisions_merged"] == 2
    card = s.get_card(a)
    contents_in_order = [h["content"] for h in card["history"]]  # newest first
    assert contents_in_order == ["一澜：回家", "一澜：到景点", "一澜：午饭", "一澜：出发"]
    assert card["current"]["content"] == "一澜：回家"  # b's latest wins overall


def test_merge_unions_folders_and_buckets_dedup_by_id():
    s = _store()
    shared = s.create_folder("旅行")
    only_a = s.create_folder("一澜收藏")
    only_b = s.create_folder("我们")
    a = s.create_card(title="A", content="x", folder_ids=[shared, only_a])
    b = s.create_card(title="B", content="y", folder_ids=[shared, only_b])
    s.add_bucket_link(a, "bucket1")
    s.add_bucket_link(a, "bucket_shared")
    s.add_bucket_link(b, "bucket_shared")  # overlap -- dedup, not duplicated
    s.add_bucket_link(b, "bucket2")
    s.merge_cards(a, b)
    folder_ids = {f["id"] for f in s.get_card_folders(a)}
    assert folder_ids == {shared, only_a, only_b}
    bucket_ids = {link["bucket_id"] for link in s.get_bucket_links(a)}
    assert bucket_ids == {"bucket1", "bucket2", "bucket_shared"}
    # the discarded card's own membership rows are gone, not duplicated
    assert s.get_card_folders(b) == s.get_card_folders(a)  # b now resolves through to a


def test_merge_unions_current_tags_without_rewriting_either_revisions_own_tags():
    s = _store()
    a = s.create_card(title="A", content="x", tags=["旅行", "云南"], valid_at="2026-01-01")
    b = s.create_card(title="B", content="y", tags=["云南", "洱海", "纪念"], valid_at="2026-01-02")
    s.merge_cards(a, b)
    card = s.get_card(a)
    assert card["current"]["tags"] == ["旅行", "云南", "洱海", "纪念"]  # union, order-stable, deduped
    # the underlying revision row's own tags column was never rewritten
    raw = [h for h in card["history"] if h["title"] == "A"][0]
    assert raw["tags"] == ["旅行", "云南"]


def test_merge_tags_override_cleared_by_next_explicit_tag_edit():
    s = _store()
    a = s.create_card(title="A", content="x", tags=["旅行"], valid_at="2026-01-01")
    b = s.create_card(title="B", content="y", tags=["洱海"], valid_at="2026-01-02")
    s.merge_cards(a, b)
    assert s.get_card(a)["current"]["tags"] == ["旅行", "洱海"]
    s.edit_current(a, tags=["新标签"])
    assert s.get_card(a)["current"]["tags"] == ["新标签"]  # override superseded, not appended to


def test_merge_attachments_all_kept_no_dedup():
    s = _store()
    a = s.create_card(title="A", content="x", attachments=[{"type": "image", "url": "/x/1.png"}])
    b = s.create_card(title="B", content="y", attachments=[{"type": "image", "url": "/x/1.png"}])  # same content, different upload
    s.merge_cards(a, b)
    total_attachments = sum(len(h["attachments"]) for h in s.get_card(a)["history"])
    assert total_attachments == 2  # both kept, no content-hash dedup (Yi Lan's call)


def test_merge_discard_id_becomes_permanent_redirect():
    s = _store()
    a = s.create_card(title="A", content="x")
    b = s.create_card(title="B", content="y")
    folder = s.create_folder("某馆")
    s.merge_cards(a, b)
    # every id-taking read/write transparently resolves the old id
    assert s.get_card(b)["id"] == a
    assert s.get_current_revision(b) == s.get_current_revision(a)
    assert s.list_revisions(b) == s.list_revisions(a)
    s.add_revision(b, content="改到旧id上", valid_at="2099-01-01")
    assert s.get_current_revision(a)["content"] == "一澜：改到旧id上"
    assert s.add_card_to_folder(b, folder)
    assert folder in [f["id"] for f in s.get_card_folders(a)]
    assert s.add_bucket_link(b, "some_bucket")
    assert "some_bucket" in [l["bucket_id"] for l in s.get_bucket_links(a)]


def test_merge_flattens_tombstone_chains_no_multi_hop_redirect():
    s = _store()
    a = s.create_card(title="A", content="x")
    b = s.create_card(title="B", content="y")
    c = s.create_card(title="C", content="z")
    s.merge_cards(a, b)  # b -> a
    s.merge_cards(c, a)  # a (and everything pointing at it) -> c
    assert s._resolve_id(a) == c
    assert s._resolve_id(b) == c  # flattened directly to c, not a two-hop chain through a
    assert s.get_card(b)["id"] == c
    assert s.get_card(a)["id"] == c


def test_merge_rejects_same_card_and_already_merged_pair():
    s = _store()
    a = s.create_card(title="A", content="x")
    b = s.create_card(title="B", content="y")
    try:
        s.merge_cards(a, a)
        assert False, "should have raised"
    except ValueError:
        pass
    s.merge_cards(a, b)
    try:
        s.merge_cards(a, b)  # b already resolves to a now
        assert False, "should have raised"
    except ValueError:
        pass


def test_all_cards_excludes_merge_tombstones():
    s = _store()
    a = s.create_card(title="A", content="x")
    b = s.create_card(title="B", content="y")
    s.merge_cards(a, b)
    ids = [c["id"] for c in s.all_cards()]
    assert ids.count(a) == 1  # not doubled up via b's tombstone resolving back to it
    assert b not in ids


def test_merge_preview_matches_actual_merge_outcome():
    s = _store()
    a = s.create_card(title="A", content="x", tags=["t1"], valid_at="2026-01-01")
    s.add_revision(a, content="x2", valid_at="2026-01-02")
    b = s.create_card(title="B", content="y", tags=["t2"], valid_at="2026-01-03")
    preview = s.merge_preview(a, b)
    assert preview["revision_count_after"] == 3
    assert preview["tags_after"] == ["t1", "t2"]
    result = s.merge_cards(a, b)
    assert result["revisions_merged"] == 1  # only b's own revision count, matching preview's card_b
    assert result["tags_after"] == preview["tags_after"]
    assert len(s.get_card(a)["history"]) == preview["revision_count_after"]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed.")


if __name__ == "__main__":
    _run_all()
