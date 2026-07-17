from datetime import datetime, timedelta

import pytest

from reminder_store import ReminderStore
from utils import LOCAL_TZ


def _store(tmp_path) -> ReminderStore:
    return ReminderStore({"state_dir": str(tmp_path / "state"), "buckets_dir": str(tmp_path / "buckets")})


def test_reminder_create_and_get_round_trips_fields(tmp_path):
    store = _store(tmp_path)

    created = store.create(title="喂猫", content="记得喂猫粮", channel="global", source="manual")

    assert created["title"] == "喂猫"
    assert created["content"] == "记得喂猫粮"
    assert created["status"] == "active"
    assert created["repeat_rule"] == "every_n_rounds"
    assert created["interval_rounds"] == 6
    fetched = store.get(created["id"])
    assert fetched == created


def test_reminder_create_requires_title_and_content(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        store.create(title="", content="内容")
    with pytest.raises(ValueError):
        store.create(title="标题", content="")


def test_reminder_list_filters_by_status(tmp_path):
    store = _store(tmp_path)
    active_id = store.create(title="活跃", content="内容")["id"]
    done_id = store.create(title="完成", content="内容")["id"]
    store.set_status(done_id, "done")

    active_items = store.list(status="active")
    all_items = store.list(status="all")

    assert [item["id"] for item in active_items] == [active_id]
    assert {item["id"] for item in all_items} == {active_id, done_id}


def test_reminder_due_respects_every_n_rounds_interval(tmp_path):
    store = _store(tmp_path)
    item = store.create(
        title="每6轮提醒",
        content="内容",
        repeat_rule="every_n_rounds",
        interval_rounds=6,
        daily_limit=0,  # isolate the round-interval check from the daily cap
    )

    # First check (round 1) with no prior reminder: due.
    due_round_1 = store.due(round_id=1, limit=5)
    assert [row["id"] for row in due_round_1] == [item["id"]]

    store.mark_reminded(item["id"], round_id=1)

    # Round 3 is within the 6-round cooldown: not due yet.
    due_round_3 = store.due(round_id=3, limit=5)
    assert due_round_3 == []

    # Round 7 (1 + 6) is due again.
    due_round_7 = store.due(round_id=7, limit=5)
    assert [row["id"] for row in due_round_7] == [item["id"]]


def test_reminder_due_respects_channel_and_session_scoping(tmp_path):
    store = _store(tmp_path)
    global_item = store.create(title="全局", content="内容", channel="global")
    scoped_item = store.create(
        title="限会话",
        content="内容",
        channel="gateway",
        session_id="sess-1",
    )

    due_other_session = store.due(session_id="sess-2", channel="gateway", round_id=1, limit=5)
    due_matching_session = store.due(session_id="sess-1", channel="gateway", round_id=1, limit=5)

    assert {row["id"] for row in due_other_session} == {global_item["id"]}
    assert {row["id"] for row in due_matching_session} == {global_item["id"], scoped_item["id"]}


def test_reminder_due_respects_daily_limit(tmp_path):
    store = _store(tmp_path)
    item = store.create(
        title="每天限一次",
        content="内容",
        repeat_rule="daily",
        daily_limit=1,
    )
    now = datetime.now(LOCAL_TZ)

    store.mark_reminded(item["id"], round_id=1, reminded_at=now.isoformat(timespec="seconds"))
    due_same_day = store.due(round_id=2, now=now, limit=5)

    assert due_same_day == []


def test_reminder_once_rule_is_not_due_again_after_first_reminder(tmp_path):
    store = _store(tmp_path)
    item = store.create(title="一次性", content="内容", repeat_rule="once")

    store.mark_reminded(item["id"], round_id=1)
    due_after = store.due(round_id=99, limit=5)

    assert due_after == []
    assert store.get(item["id"])["status"] == "archived"


def test_reminder_snooze_moves_next_due_at_forward(tmp_path):
    store = _store(tmp_path)
    item = store.create(title="稍后提醒", content="内容")

    snoozed = store.snooze(item["id"], minutes=30)

    assert snoozed["status"] == "active"
    next_due = datetime.fromisoformat(snoozed["next_due_at"])
    assert next_due > datetime.now(LOCAL_TZ) + timedelta(minutes=20)


def test_reminder_archive_expired_marks_past_end_at(tmp_path):
    store = _store(tmp_path)
    item = store.create(title="已过期", content="内容", end_at="2020-01-01")

    expired_ids = store.archive_expired()

    assert item["id"] in expired_ids
    assert store.get(item["id"])["status"] == "archived"


def test_reminder_update_rejects_invalid_status(tmp_path):
    store = _store(tmp_path)
    item = store.create(title="标题", content="内容")

    with pytest.raises(ValueError):
        store.update(item["id"], status="not_a_real_status")


def test_reminder_update_returns_none_for_missing_id(tmp_path):
    store = _store(tmp_path)

    assert store.update("does-not-exist", title="x") is None
    assert store.set_status("does-not-exist", "done") is None
