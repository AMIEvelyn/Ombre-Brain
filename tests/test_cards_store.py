"""Isolated tests for cards_store.CardStore (facts-model v2 data layer).

Pure stdlib + sqlite -- no project deps, no network. Runs under pytest, or
standalone: `python3 tests/test_cards_store.py`.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards_store import CardStore  # noqa: E402


def _store():
    tmp = tempfile.mkdtemp()
    return CardStore(db_path=os.path.join(tmp, "cards.sqlite"))


def test_create_card_and_current():
    s = _store()
    cid = s.create_card(title="身高", content="170cm", tags=["体征"], valid_at="2026-01-01")
    assert cid.startswith("F")
    cur = s.get_current_revision(cid)
    assert cur["title"] == "身高" and cur["content"] == "170cm"
    assert cur["tags"] == ["体征"]
    card = s.get_card(cid)
    assert card["current"]["content"] == "170cm"
    assert len(card["history"]) == 1


def test_new_revision_latest_wins_and_carries_title():
    s = _store()
    cid = s.create_card(title="身高", content="170cm", valid_at="2026-01-01")
    # New Revision only supplies content + a newer date; title carries over.
    s.add_revision(cid, content="175cm", valid_at="2026-06-01")
    cur = s.get_current_revision(cid)
    assert cur["content"] == "175cm"       # latest valid_at wins
    assert cur["title"] == "身高"           # title carried from previous revision
    hist = s.list_revisions(cid)
    assert len(hist) == 2
    assert [h["content"] for h in hist] == ["175cm", "170cm"]  # newest first


def test_backdated_revision_is_not_current():
    s = _store()
    cid = s.create_card(title="体重", content="100斤", valid_at="2026-06-01")
    s.add_revision(cid, content="98斤", valid_at="2026-03-01")  # older event date
    assert s.get_current_revision(cid)["content"] == "100斤"     # newest date still current


def test_edit_current_in_place_no_new_timepoint():
    s = _store()
    cid = s.create_card(title="酸辣粉", content="爱吃", valid_at="2026-01-01")
    ok = s.edit_current(cid, title="酸辣粉（重庆）", content="超爱吃")
    assert ok
    assert len(s.list_revisions(cid)) == 1            # still ONE revision
    cur = s.get_current_revision(cid)
    assert cur["title"] == "酸辣粉（重庆）" and cur["content"] == "超爱吃"


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
    assert hk_entry["content"] == "回家"
    assert hk_entry["revision_count"] == 2                     # but knows it has history


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
