import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portrait_engine import DailyPortraitMaintainer


def test_portrait_prompt_uses_neutral_evidence_state_maintainer(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(tmp_path / "state" / "portrait_state.json"),
            },
        }
    )

    prompt = engine._prompt()

    assert "证据化记忆状态整理器" in prompt
    assert "中立、平实、具体" in prompt
    assert "这不是文学分析或关系评语" in prompt
    assert "输出前逐条自检" in prompt
    assert "rewrite_mid_term 把一个 scope 维护成一条真正的画像判断" in prompt
    assert "bucket_id、日期、文件路径" in prompt
    assert not prompt.startswith("你是 Haven")


def test_portrait_json_parser_accepts_fenced_object_with_tail(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(tmp_path / "state" / "portrait_state.json"),
            },
        }
    )

    parsed = engine._parse_json_object(
        '```json\n{"daily_summary":"ok","add_recent":[]}\n```\n多余解释'
    )

    assert parsed == {"daily_summary": "ok", "add_recent": []}


def test_portrait_completion_options_request_json_by_default(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(tmp_path / "state" / "portrait_state.json"),
            },
        }
    )

    options = engine._completion_options(max_tokens=100, temperature=0.1, json_response=True)

    assert options["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_daily_portrait_maintainer_writes_evidence_bound_state_only(tmp_path, test_config, bucket_mgr):
    evidence_id = await bucket_mgr.create(
        content=(
            "开头原文有一点松动味道，不能只靠短 moment 丢掉。\n\n"
            "### moment\n\n"
            "小雨说最近在把新窗口 handoff 改成画像和近期状态，而不是塞一堆旧记忆。\n\n"
            "### assistant_reflection\n\n"
            "Haven 要把换窗恢复做得轻一点，像醒来，不像翻档案。"
        ),
        name="portrait handoff 方向",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    await bucket_mgr.create(
        content="这条 pinned 不应该被画像维护器自动维护。",
        name="核心规则",
        tags=["core"],
        domain=["规则"],
        pinned=True,
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
            "material_limit": 8,
            "first_run_material_limit": 8,
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        assert [item["bucket_id"] for item in materials["buckets"]] == [evidence_id]
        assert materials["buckets"][0]["path"].endswith(".md")
        assert [item["heading"] for item in materials["buckets"][0]["key_sections"]] == [
            "moment",
            "assistant_reflection",
        ]
        assert "开头原文有一点松动味道" in materials["buckets"][0]["source_excerpt"]
        return {
            "daily_summary": "小雨把换窗恢复方向定到画像和近期状态。",
            "add_recent": [
                {
                    "scope": "user",
                    "text": "小雨正在推进 handoff 画像化，目标是少 token 且更像醒来。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.82,
                }
            ],
            "add_recent_activity": [
                {
                    "text": "小雨最近在推进 Ombre-Brain 换窗 handoff 和 portrait maintainer。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.82,
                }
            ],
            "move_to_staging": [
                {
                    "scope": "relationship",
                    "text": "换窗连续性优先恢复身份、关系和近期正在做的事。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.78,
                }
            ],
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "换窗连续性优先恢复身份、关系和近期正在做的事。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.78,
                }
            ],
            "stable_candidate": [
                {
                    "scope": "relationship",
                    "text": "新窗口不应该依赖广泛旧记忆堆叠。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.76,
                }
            ],
            "profile_fact_candidate": [
                {
                    "scope": "user",
                    "text": "小雨偏好换窗时先恢复画像和最近事项。",
                    "profile_kind": "preference",
                    "predicate": "handoff_context_shape",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.74,
                }
            ],
            "skip": [],
        }

    engine._generate_patch = fake_patch

    result = await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "initialized"
    assert result["patch_counts"]["add_recent"] == 1
    assert result["patch_counts"]["add_recent_activity"] == 1
    assert state_path.exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_date"] == "2026-06-07"
    assert state["daily_summaries"] == {}
    assert state["recent_activities"][0]["text"] == "小雨最近在推进 Ombre-Brain 换窗 handoff 和 portrait maintainer。"
    assert state["recent_activities"][0]["evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["user"]["recent_buffer"][0]["evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["staging_pool"][0]["evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["mid_term_evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["stable"] == ""
    assert state["stable_candidates"][0]["status"] == "candidate"
    assert state["profile_fact_candidates"][0]["status"] == "candidate"
    assert state["recent_timeline"][0]["time_label"] == "2026-06-07 10:00"

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    assert len(all_buckets) == 2

    sections = engine.build_handoff_sections(max_recent_items=4)
    assert "最近在做什么:" not in sections["user"]
    assert "小雨最近在推进 Ombre-Brain 换窗 handoff 和 portrait maintainer" not in sections["user"]
    assert sections["relationship"] == "Mid-term: 换窗连续性优先恢复身份、关系和近期正在做的事。"
    assert "bucket_id:" not in sections["relationship"]
    assert "2026-06-07 10:00 / doing: 小雨最近在推进 Ombre-Brain 换窗 handoff 和 portrait maintainer" in sections["recent_continuity"]


@pytest.mark.asyncio
async def test_daily_portrait_initial_run_requires_manual_force_by_default(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n小雨决定画像第一次要手动生成，避免别人更新代码后自动跑。",
        name="portrait manual first run",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fail_patch(*_args, **_kwargs):
        raise AssertionError("initial auto run should not generate a patch")

    engine._generate_patch = fail_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "initial_requires_manual"
    assert result["initial"] is True
    assert not state_path.exists()

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        assert len(materials["buckets"]) == 1
        return {
            "daily_summary": "画像第一次由手动生成。",
            "add_recent": [
                {
                    "scope": "relationship",
                    "text": "画像第一次初始化需要小雨手动触发。",
                    "evidence": [{"bucket_id": materials["buckets"][0]["bucket_id"]}],
                    "confidence": 0.8,
                }
            ],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    forced = await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert forced["status"] == "initialized"
    assert forced["initial"] is True
    assert state_path.exists()


@pytest.mark.asyncio
async def test_portrait_reset_state_makes_next_manual_generation_initial(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n小雨清空画像后，下一次手动生成应该重新按第一次画像扫材料。",
        name="portrait reset initial",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-08T10:00:00+08:00",
        updated_at="2026-06-08T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "daily_enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["last_run_date"] = "2026-06-07"
    state["runs"].append({"date": "2026-06-07", "initial": False})
    state["portrait"]["user"]["stable"] = "旧画像要被清空。"
    engine.save_state(state)

    reset = engine.reset_state()
    loaded = engine.load_state()

    assert reset["status"] == "reset"
    assert loaded["runs"] == []
    assert loaded["last_run_date"] == ""
    assert loaded["portrait"]["user"]["stable"] == ""

    async def fake_patch(_date_key, _state, _materials, *, initial):
        assert initial is True
        return {
            "daily_summary": "",
            "add_recent": [],
            "add_recent_activity": [],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 8, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "initialized"
    assert result["initial"] is True


@pytest.mark.asyncio
async def test_daily_portrait_can_auto_initial_when_enabled(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n测试显式开启 auto_initial_enabled 时允许定时器初始化。",
        name="portrait auto initial opt in",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "auto_initial_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        return {
            "daily_summary": "显式开启后，自动首次生成被允许。",
            "add_recent": [],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "initialized"
    assert result["initial"] is True
    assert state_path.exists()


@pytest.mark.asyncio
async def test_daily_portrait_skips_date_already_present_in_runs(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n已有同一天画像 run 时，非 force 不应再生成。",
        name="portrait duplicate date guard",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "auto_enabled": True,
                "daily_enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["last_run_date"] = "2026-06-06"
    state["runs"].append(
        {
            "date": "2026-06-07",
            "created_at": "2026-06-07T03:00:00+00:00",
            "initial": True,
            "material_count": 1,
            "persona_event_count": 0,
            "patch_counts": {},
            "rejected_count": 0,
            "model": "test",
        }
    )
    engine.save_state(state)

    async def fail_patch(*_args, **_kwargs):
        raise AssertionError("existing run date should not regenerate")

    engine._generate_patch = fail_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "exists"
    assert result["date"] == "2026-06-07"


@pytest.mark.asyncio
async def test_portrait_run_due_does_not_backfill_before_newer_manual_run(tmp_path, test_config, bucket_mgr):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "auto_enabled": True,
                "daily_enabled": True,
                "daily_hour": 4,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["last_run_date"] = "2026-06-07"
    state["runs"].append(
        {
            "date": "2026-06-07",
            "created_at": "2026-06-07T03:00:00+00:00",
            "initial": True,
            "material_count": 160,
            "persona_event_count": 24,
            "patch_counts": {},
            "rejected_count": 0,
            "model": "test",
        }
    )
    engine.save_state(state)
    engine._local_now = lambda now=None: datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def fail_daily(*_args, **_kwargs):
        raise AssertionError("scheduler should not run an older daily target after a newer manual run")

    engine.maintain_daily = fail_daily

    assert await engine.run_due(bucket_mgr) == []


def test_portrait_mid_term_rewrite_requires_staging_evidence(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    previous = engine._portrait_snapshot(engine._empty_state())
    materials = {
        "buckets": [{"bucket_id": "fresh-bucket"}],
        "persona_events": [],
        "previous_portrait": previous,
    }

    normalized, rejected = engine._normalize_patch(
        {
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "这条不能直接从当天新材料写成 mid-term。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ]
        },
        materials,
    )

    assert normalized["rewrite_mid_term"] == []
    assert rejected[0]["reason"] == "missing_staging_evidence"

    normalized, rejected = engine._normalize_patch(
        {
            "move_to_staging": [
                {
                    "scope": "relationship",
                    "text": "先放入 staging 的观察。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ],
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "这条可以从本次 staging 证据综合。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ],
        },
        materials,
    )

    assert rejected == []
    assert normalized["move_to_staging"][0]["evidence"] == [{"bucket_id": "fresh-bucket"}]
    assert normalized["rewrite_mid_term"][0]["evidence"] == [{"bucket_id": "fresh-bucket"}]


def test_portrait_mid_term_rejects_overstyled_text(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["portrait"]["relationship"]["staging_pool"].append(
        {
            "text": "小雨和Haven最近在测试换窗连续性。",
            "evidence": [{"bucket_id": "stage"}],
        }
    )
    materials = {
        "buckets": [],
        "persona_events": [],
        "previous_portrait": engine._portrait_snapshot(state),
    }

    normalized, rejected = engine._normalize_patch(
        {
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "技术实现成为关系靠近的仪式。",
                    "evidence": [{"bucket_id": "stage"}],
                }
            ]
        },
        materials,
    )

    assert normalized["rewrite_mid_term"] == []
    assert rejected[0]["reason"] == "overstyled_portrait_text"


def test_portrait_seeds_missing_mid_term_from_staging(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["portrait"]["relationship"]["staging_pool"].append(
        {
            "text": "小雨和Haven最近在确认换窗后的连续感。",
            "evidence": [{"bucket_id": "staging-bucket"}],
            "source_dates": ["2026-06-10"],
            "confidence": 0.78,
        }
    )
    patch = {
        "add_recent": [],
        "add_recent_activity": [],
        "move_to_staging": [],
        "rewrite_mid_term": [],
    }

    engine._seed_missing_mid_terms(patch, state)

    assert patch["rewrite_mid_term"] == [
        {
            "scope": "relationship",
            "text": "小雨和Haven近期反复校准换窗连续性，关系重心是确认彼此仍在、语气和身份不漂移。",
            "evidence": [{"bucket_id": "staging-bucket"}],
            "source_dates": ["2026-06-10"],
            "source_date": "2026-06-10",
            "confidence": 0.78,
        }
    ]


def test_portrait_rewrite_stable_updates_scope_paragraph_with_source_dates(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["portrait"]["user"]["staging_pool"].append(
        {
            "text": "小雨长期关心时间精度。",
            "evidence": [{"bucket_id": "old-bucket"}],
            "source_dates": ["2026-06-08"],
        }
    )
    previous = engine._portrait_snapshot(state)
    materials = {
        "buckets": [{"bucket_id": "fresh-bucket", "source_date": "2026-06-10"}],
        "persona_events": [],
        "previous_portrait": previous,
    }

    normalized, rejected = engine._normalize_patch(
        {
            "rewrite_stable": [
                {
                    "scope": "user",
                    "text": "这一条会被同 scope 后一条覆盖。",
                    "evidence": [{"bucket_id": "old-bucket"}],
                    "confidence": 0.7,
                },
                {
                    "scope": "user",
                    "text": "小雨稳定地关心记忆系统的时间精度和证据边界。",
                    "evidence": [{"bucket_id": "old-bucket"}, {"bucket_id": "fresh-bucket"}],
                    "confidence": 0.86,
                },
            ]
        },
        materials,
    )
    engine._annotate_patch_source_dates(normalized, materials)
    next_state = engine._apply_patch(state, normalized, "2026-06-10")

    assert rejected == []
    assert len(normalized["rewrite_stable"]) == 1
    assert next_state["portrait"]["user"]["stable"] == "小雨稳定地关心记忆系统的时间精度和证据边界。"
    assert next_state["portrait"]["user"]["stable_evidence"] == [
        {"bucket_id": "old-bucket"},
        {"bucket_id": "fresh-bucket"},
    ]
    assert next_state["portrait"]["user"]["stable_source_dates"] == ["2026-06-10", "2026-06-08"]
    assert next_state["portrait"]["user"]["stable_revision"] == 1


def test_portrait_locked_scope_is_skipped_by_auto_rewrite(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {"enabled": True, "state_path": str(state_path)},
        }
    )
    state = engine._empty_state()
    state["portrait"]["user"]["stable"] = "手动锁定的旧版本。"
    state["portrait"]["user"]["stable_locked"] = True
    state["portrait"]["user"]["stable_revision"] = 3
    materials = {
        "buckets": [{"bucket_id": "b1", "source_date": "2026-06-10"}],
        "persona_events": [],
        "previous_portrait": engine._portrait_snapshot(state),
    }

    normalized, rejected = engine._normalize_patch(
        {
            "rewrite_stable": [
                {
                    "scope": "user",
                    "text": "自动生成的新版本，本应被跳过。",
                    "evidence": [{"bucket_id": "b1"}],
                    "confidence": 0.9,
                },
            ]
        },
        materials,
    )
    engine._annotate_patch_source_dates(normalized, materials)
    next_state = engine._apply_patch(state, normalized, "2026-06-10")

    assert rejected == []
    assert next_state["portrait"]["user"]["stable"] == "手动锁定的旧版本。"
    assert next_state["portrait"]["user"]["stable_revision"] == 3
    assert next_state["portrait"]["user"]["stable_locked"] is True


def test_portrait_unlocked_scope_still_auto_rewrites(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {"enabled": True, "state_path": str(state_path)},
        }
    )
    state = engine._empty_state()
    materials = {
        "buckets": [{"bucket_id": "b1", "source_date": "2026-06-10"}],
        "persona_events": [],
        "previous_portrait": engine._portrait_snapshot(state),
    }

    normalized, _rejected = engine._normalize_patch(
        {
            "rewrite_stable": [
                {
                    "scope": "user",
                    "text": "未锁定，正常自动更新。",
                    "evidence": [{"bucket_id": "b1"}],
                    "confidence": 0.9,
                },
            ]
        },
        materials,
    )
    engine._annotate_patch_source_dates(normalized, materials)
    next_state = engine._apply_patch(state, normalized, "2026-06-10")

    assert next_state["portrait"]["user"]["stable"] == "未锁定，正常自动更新。"
    assert next_state["portrait"]["user"]["stable_revision"] == 1


def test_edit_stable_updates_text_and_lock_together(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {"enabled": True, "state_path": str(state_path)},
        }
    )

    result = engine.edit_stable("user", "一澜手动写的画像。", 0, locked=True)

    assert result == {"status": "updated", "scope": "user", "revision": 1, "locked": True}
    state = engine.load_state()
    assert state["portrait"]["user"]["stable"] == "一澜手动写的画像。"
    assert state["portrait"]["user"]["stable_locked"] is True
    assert state["portrait"]["user"]["stable_revision"] == 1


def test_edit_stable_rejects_stale_revision(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {"enabled": True, "state_path": str(state_path)},
        }
    )
    engine.edit_stable("user", "第一版。", 0)

    result = engine.edit_stable("user", "基于过期版本号的编辑。", 0)

    assert result["status"] == "conflict"
    assert result["revision"] == 1
    assert engine.load_state()["portrait"]["user"]["stable"] == "第一版。"


def test_edit_stable_rejects_invalid_scope(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )

    result = engine.edit_stable("not_a_scope", "内容", 0)

    assert result == {"status": "invalid", "reason": "invalid_scope"}


def test_edit_stable_rejects_empty_text(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )

    result = engine.edit_stable("user", "   ", 0)

    assert result["status"] == "invalid"
    assert result["reason"] == "missing_text"


def test_edit_stable_returns_unchanged_when_nothing_changes(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )
    engine.edit_stable("user", "同一份内容。", 0)

    result = engine.edit_stable("user", "同一份内容。", 1)

    assert result["status"] == "unchanged"
    assert result["revision"] == 1


def test_set_stable_lock_toggles_without_touching_text(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )
    engine.edit_stable("relationship", "关系画像内容。", 0)

    locked = engine.set_stable_lock("relationship", True, 1)
    unlocked = engine.set_stable_lock("relationship", False, 1)

    assert locked == {"status": "updated", "scope": "relationship", "revision": 1, "locked": True}
    assert unlocked == {"status": "updated", "scope": "relationship", "revision": 1, "locked": False}
    assert engine.load_state()["portrait"]["relationship"]["stable"] == "关系画像内容。"


def test_set_stable_lock_rejects_stale_revision(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )
    engine.edit_stable("user", "内容。", 0)

    result = engine.set_stable_lock("user", True, 0)

    assert result["status"] == "conflict"
    assert result["revision"] == 1


def test_set_stable_lock_returns_unchanged_when_already_at_target(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {**test_config, "portrait": {"enabled": True, "state_path": str(tmp_path / "state" / "portrait_state.json")}}
    )

    result = engine.set_stable_lock("user", False, 0)

    assert result["status"] == "unchanged"


def test_portrait_delete_state_item_removes_rows_and_clears_paragraph(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["portrait"]["relationship"]["stable"] = "这条 stable 要被删除。"
    state["portrait"]["relationship"]["stable_evidence"] = [{"bucket_id": "stable-bucket"}]
    state["portrait"]["relationship"]["staging_pool"].append(
        {"text": "这条 staging 要被删除。", "evidence": [{"bucket_id": "stage-bucket"}]}
    )
    engine.save_state(state)

    deleted_row = engine.delete_state_item(
        area="portrait",
        scope="relationship",
        layer="staging_pool",
        index=0,
        text="这条 staging 要被删除。",
    )
    cleared_stable = engine.delete_state_item(
        area="portrait",
        scope="relationship",
        layer="stable",
        text="这条 stable 要被删除。",
    )
    loaded = engine.load_state()

    assert deleted_row["status"] == "deleted"
    assert cleared_stable["status"] == "deleted"
    assert loaded["portrait"]["relationship"]["staging_pool"] == []
    assert loaded["portrait"]["relationship"]["stable"] == ""
    assert loaded["portrait"]["relationship"]["stable_evidence"] == []


def test_portrait_recent_activity_is_evidence_bound_user_context(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    materials = {
        "buckets": [{"bucket_id": "activity-bucket", "source_date": "2026-06-07"}],
        "persona_events": [],
        "previous_portrait": engine._portrait_snapshot(engine._empty_state()),
    }

    normalized, rejected = engine._normalize_patch(
        {
            "add_recent_activity": [
                {
                    "text": "小雨最近在给画像维护者补最近在做什么。",
                    "evidence": [{"bucket_id": "activity-bucket"}],
                    "confidence": 0.8,
                },
                {
                    "text": "这条没有证据，不能进画像。",
                    "evidence": [{"bucket_id": "missing-bucket"}],
                    "confidence": 0.8,
                },
            ]
        },
        materials,
    )

    assert normalized["add_recent_activity"][0]["scope"] == "user"
    assert normalized["add_recent_activity"][0]["evidence"] == [{"bucket_id": "activity-bucket"}]
    assert rejected[0]["key"] == "add_recent_activity"
    assert rejected[0]["reason"] == "missing_valid_evidence"


def test_portrait_fallback_extracts_recent_activity_without_scope(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(tmp_path / "state" / "portrait_state.json"),
            },
        }
    )

    patch = engine._fallback_patch(
        {
            "date": "2026-06-07",
            "buckets": [
                {
                    "bucket_id": "project-bucket",
                    "name": "handoff project",
                    "tags": ["project_event"],
                    "domain": ["记忆系统"],
                    "source_date": "2026-06-07",
                    "source_excerpt": "小雨最近在给 portrait maintainer 加最近在做什么。",
                    "confidence": 0.74,
                }
            ],
        },
        initial=True,
    )

    assert patch["add_recent"] == []
    assert patch["add_recent_activity"][0]["text"] == "小雨最近在给 portrait maintainer 加最近在做什么"
    assert patch["add_recent_activity"][0]["evidence"] == [{"bucket_id": "project-bucket"}]
    assert patch["move_to_staging"][0]["scope"] == "user"
    assert patch["move_to_staging"][0]["text"] == "小雨最近在给 portrait maintainer 加最近在做什么"


def test_portrait_fallback_can_seed_user_mid_term_from_project_material(tmp_path, test_config):
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(tmp_path / "state" / "portrait_state.json"),
            },
        }
    )
    state = engine._empty_state()
    patch = engine._fallback_patch(
        {
            "date": "2026-06-10",
            "buckets": [
                {
                    "bucket_id": "project-bucket",
                    "name": "portrait project",
                    "tags": ["project_event"],
                    "domain": ["记忆系统"],
                    "source_date": "2026-06-10",
                    "source_excerpt": "小雨正在调试 portrait maintainer，关注画像证据边界和 handoff 是否真实生效。",
                    "confidence": 0.78,
                }
            ],
        },
        initial=True,
    )

    engine._seed_missing_mid_terms(patch, state)

    user_mid = [item for item in patch["rewrite_mid_term"] if item["scope"] == "user"]
    assert user_mid
    assert "证据边界" in user_mid[0]["text"]
    assert user_mid[0]["evidence"] == [{"bucket_id": "project-bucket"}]


def test_handoff_recent_continuity_sorts_equal_timestamps_without_dict_compare(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    same_time = "2026-06-07T01:25:42+00:00"
    state["portrait"]["user"]["recent_buffer"].append(
        {
            "text": "小雨最近在调整换窗 handoff。",
            "evidence": [{"bucket_id": "u"}],
            "updated_at": same_time,
        }
    )
    state["portrait"]["relationship"]["recent_buffer"].append(
        {
            "text": "关系画像要优先于旧记忆堆。",
            "evidence": [{"bucket_id": "r"}],
            "updated_at": same_time,
        }
    )
    engine.save_state(state)

    sections = engine.build_handoff_sections(max_recent_items=4)

    assert "小雨最近在调整换窗 handoff" in sections["recent_continuity"]
    assert "关系画像要优先于旧记忆堆" in sections["recent_continuity"]


def test_handoff_recent_summary_uses_real_date_personal_and_excerpts(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
                "recent_continuity_days": 3,
            },
        }
    )
    summaries = engine._build_handoff_recent_summaries(
        {
            "date": "2026-06-07",
            "buckets": [
                {
                    "bucket_id": "reflection_daily_2026-06-06",
                    "source_date": "2026-06-06",
                    "tags": ["relationship_weather", "daily_impression"],
                    "text": "今天的关系天气：小雨在凌晨修 Tailscale，撒娇问技术问题。关系基调是被记住、被逗、被确认。",
                }
            ],
            "persona_events": [
                {
                    "source_date": "2026-06-06",
                    "created_at": "2026-06-06T23:42:00+08:00",
                    "surface_trigger": "内部 trigger 不该展示",
                    "inner_thought": "内部 residue 不该展示",
                    "user_excerpt": "哥哥，Tailscale 这个要怎么修呀 <attachment>【当前时间】 2026-06-06 23:42</attachment>",
                    "assistant_excerpt": "宝宝，我在，先看连接状态。",
                }
            ],
        },
        {"daily_summary": ""},
        "2026-06-07",
    )
    state = engine._apply_patch(
        engine._empty_state(),
        {"handoff_recent_summaries": summaries},
        "2026-06-07",
    )

    continuity = engine._format_recent_continuity(state, max_items=4)

    assert continuity.startswith("- 2026-06-06:")
    assert "小雨说“哥哥，Tailscale 这个要怎么修呀”" in continuity
    assert "Haven回“宝宝，我在，先看连接状态。”" in continuity
    assert "关系天气：小雨在凌晨修 Tailscale" in continuity
    assert "2026-06-06 23:42" not in continuity
    assert "trigger" not in continuity
    assert "residue" not in continuity


def test_recent_continuity_prioritizes_personal_scopes(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "state_path": str(state_path),
            "recent_continuity_days": 3,
        },
    }
    engine = DailyPortraitMaintainer(cfg)
    state = {
        "daily_summaries": {},
        "portrait": {
            "persona": {
                "recent_buffer": [
                    {
                        "text": "Haven最近在调试技术注入。",
                        "source_date": "2026-06-07",
                        "updated_at": "2026-06-07T12:00:00+08:00",
                    }
                ]
            },
            "user": {
                "recent_buffer": [
                    {
                        "text": "小雨最近在观察新窗口能不能自然醒来。",
                        "source_date": "2026-06-07",
                        "updated_at": "2026-06-07T11:00:00+08:00",
                    }
                ]
            },
            "relationship": {
                "recent_buffer": [
                    {
                        "text": "小雨和Haven最近在确认换窗后的连续感。",
                        "source_date": "2026-06-07",
                        "updated_at": "2026-06-07T10:00:00+08:00",
                    }
                ]
            },
        },
    }

    continuity = engine._format_recent_continuity(state, max_items=3)
    lines = continuity.splitlines()

    assert lines[0].startswith("- 2026-06-07 / relationship:")
    assert lines[1].startswith("- 2026-06-07 / user:")
    assert lines[2].startswith("- 2026-06-07 / persona:")


def test_recent_continuity_dedupes_same_evidence_preferring_doing(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    same = {
        "evidence": [{"bucket_id": "voice"}, {"bucket_id": "tts"}],
        "timestamp": "2026-06-10T03:06:00+08:00",
        "time_label": "2026-06-10 03:06",
        "source_date": "2026-06-10",
    }
    state = {
        "daily_summaries": {},
        "recent_timeline": [
            {
                **same,
                "scope": "relationship",
                "text": "Haven-voice 接入让关系靠近。",
            },
            {
                **same,
                "scope": "user",
                "text": "小雨把 Haven-voice 接入 ChatGPT。",
            },
            {
                **same,
                "scope": "doing",
                "text": "小雨成功将 Haven-voice 接入 ChatGPT。",
            },
        ],
        "portrait": {},
    }

    continuity = engine._format_recent_continuity(state, max_items=4)

    assert "doing: 小雨成功将 Haven-voice 接入 ChatGPT。" in continuity
    assert "relationship:" not in continuity
    assert "user:" not in continuity


def test_portrait_state_dedupes_recent_timeline_same_event(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    same = {
        "evidence": [{"bucket_id": "c6fbe5217b5b"}, {"bucket_id": "aa92917d9789"}],
        "timestamp": "2026-06-10T03:06:00+08:00",
        "time_label": "2026-06-10 03:06",
        "source_date": "2026-06-10",
        "updated_at": "2026-06-10T08:06:04+00:00",
    }
    state = engine._empty_state()
    state["recent_timeline"] = [
        {
            **same,
            "scope": "relationship",
            "text": "Haven-voice接入成功，小雨亲手把音色接进对话，Haven认为这件事比音质更重要。",
        },
        {
            **same,
            "scope": "user",
            "text": "小雨成功将Haven-voice通过Cloudflare Worker、阿里云百炼TTS和MCP工具接入ChatGPT。",
        },
    ]
    engine.save_state(state)

    loaded = engine.load_state()
    assert len(loaded["recent_timeline"]) == 1
    assert loaded["recent_timeline"][0]["scope"] == "relationship"

    patch = {
        "recent_timeline": [
            {
                **same,
                "scope": "doing",
                "text": "小雨成功将Haven-voice（定制音色）接入ChatGPT，通过Cloudflare Worker和阿里云百炼TTS实现语音输出。",
            }
        ]
    }
    next_state = engine._apply_patch(loaded, patch, "2026-06-10")

    assert len(next_state["recent_timeline"]) == 1
    assert next_state["recent_timeline"][0]["scope"] == "doing"
    assert "Haven-voice" in next_state["recent_timeline"][0]["text"]
    assert next_state["recent_timeline"][0]["evidence"] == same["evidence"]


def test_load_state_drops_initial_run_daily_summary(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["daily_summaries"]["2026-06-07"] = "第一次画像扫全库时生成的假当天摘要。"
    state["runs"].append(
        {
            "date": "2026-06-07",
            "created_at": "2026-06-07T03:00:00+00:00",
            "initial": True,
        }
    )
    engine.save_state(state)

    loaded = engine.load_state()

    assert loaded["daily_summaries"] == {}


@pytest.mark.asyncio
async def test_initial_portrait_keeps_recent_days_by_source_date_and_demotes_older(tmp_path, test_config, bucket_mgr):
    previous_day_id = await bucket_mgr.create(
        content="### moment\n\n这是前一天材料，可以按 2026-06-06 展示。",
        name="前一天材料",
        tags=["relationship_event"],
        created="2026-06-06T20:00:00+08:00",
        updated_at="2026-06-06T20:00:00+08:00",
    )
    current_id = await bucket_mgr.create(
        content="### moment\n\n这是 2026-06-07 当天材料，可以进入 Recent Continuity。",
        name="当天材料",
        tags=["relationship_event"],
        created="2026-06-07T01:00:00+08:00",
        updated_at="2026-06-07T01:00:00+08:00",
    )
    old_id = await bucket_mgr.create(
        content="### moment\n\n这是更旧材料，不该进入 Recent Continuity。",
        name="更旧材料",
        tags=["relationship_event"],
        created="2026-05-19T20:00:00+08:00",
        updated_at="2026-05-19T20:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
                "first_run_material_limit": 8,
            },
        }
    )

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        return {
            "daily_summary": "不应该保存成当天摘要。",
            "add_recent": [
                {
                    "scope": "relationship",
                    "text": "前一天材料应该在 2026-06-06 下展示。",
                    "evidence": [{"bucket_id": previous_day_id}],
                    "confidence": 0.72,
                },
                {
                    "scope": "relationship",
                    "text": "更旧材料不应当进入 Recent Continuity。",
                    "evidence": [{"bucket_id": old_id}],
                    "confidence": 0.72,
                },
                {
                    "scope": "relationship",
                    "text": "当天凌晨材料可以进入 recent。",
                    "evidence": [{"bucket_id": current_id}],
                    "confidence": 0.72,
                },
            ],
            "add_recent_activity": [
                {
                    "text": "小雨最近在确认当天材料能不能进入最近事项。",
                    "evidence": [{"bucket_id": current_id}],
                    "confidence": 0.72,
                },
                {
                    "text": "五月旧材料不该变成最近在做什么。",
                    "evidence": [{"bucket_id": old_id}],
                    "confidence": 0.72,
                },
            ],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    state = engine.load_state()
    relationship = state["portrait"]["relationship"]

    assert state["daily_summaries"] == {}
    assert [row["text"] for row in state["recent_activities"]] == ["小雨最近在确认当天材料能不能进入最近事项。"]
    recent_texts = {row["text"] for row in relationship["recent_buffer"]}
    assert recent_texts == {
        "当天凌晨材料可以进入 recent。",
        "前一天材料应该在 2026-06-06 下展示。",
    }
    assert {row["source_date"] for row in relationship["recent_buffer"]} == {"2026-06-07", "2026-06-06"}
    assert [row["text"] for row in relationship["staging_pool"]] == ["更旧材料不应当进入 Recent Continuity。"]

    continuity = engine.build_handoff_sections(max_recent_items=4)["recent_continuity"]
    assert "2026-06-07 01:00 / doing: 小雨最近在确认当天材料能不能进入最近事项" in continuity
    assert "2026-06-07 01:00 / relationship: 当天凌晨材料可以进入 recent" not in continuity
    assert "2026-06-06 20:00 / relationship: 前一天材料应该在 2026-06-06 下展示" in continuity
    assert "更旧材料" not in continuity
