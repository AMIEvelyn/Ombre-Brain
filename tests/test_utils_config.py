from datetime import datetime
from zoneinfo import ZoneInfo

from utils import local_date_key, load_config, parse_human_date_reference, strip_human_date_references


def test_load_config_defaults_relationship_weather_off(tmp_path):
    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["gateway"]["relationship_weather_interval_rounds"] == 0
    assert config["gateway"]["cooldown_hours"] == 6
    assert config["gateway"]["skip_recent_rounds"] == 5
    assert config["gateway"]["semantic_session_dedupe_enabled"] is True
    assert config["gateway"]["semantic_session_dedupe_threshold"] == 0.90
    assert config["gateway"]["semantic_session_dedupe_lexical_threshold"] == 0.82
    assert config["gateway"]["portrait_memory_include_anchors"] is False
    assert config["self_anchor"]["entry_bucket_id"] == ""
    assert config["write_path"]["semantic_search_timeout_seconds"] == 3
    assert config["memory_write_gate"]["auto_sources"] == ["operit", "workflow", "worker", "auto"]
    assert config["memory_write_gate"]["repeat_promote_count"] == 2
    assert config["raw_events"]["db_path"] == ""
    assert config["raw_events"]["max_ingest_batch"] == 1000
    assert config["word_map"]["daily_rebuild_enabled"] is True
    assert config["word_map"]["daily_rebuild_hour"] == 4
    assert config["word_map"]["daily_rebuild_minute"] == 30
    assert config["word_map"]["daily_rebuild_include_archive"] is False
    assert config["word_map"]["daily_rebuild_check_interval_minutes"] == 15
    assert config["reflection"]["enrich_backfill_enabled"] is True
    assert config["reflection"]["enrich_backfill_limit"] == 5
    assert config["reflection"]["edge_backfill_limit"] == 5
    assert config["reflection"]["daily_enabled"] is True
    assert config["reflection"]["daily_min_memory_items"] == 5
    assert config["reflection"]["daily_conversation_turn_limit"] == 0
    assert config["reflection"]["memory_affect_anchor_enabled"] is True
    assert config["reflection"]["relationship_weather_affect_anchor_enabled"] is True
    assert config["portrait"]["enabled"] is True
    assert config["portrait"]["auto_enabled"] is True
    assert config["portrait"]["daily_enabled"] is True
    assert config["portrait"]["state_path"] == ""
    assert config["dream"]["old_echo_enabled"] is True
    assert config["dream"]["old_echo_min_age_hours"] == 72


def test_load_config_reads_runtime_config_before_env_override(tmp_path, monkeypatch):
    runtime_path = tmp_path / "state" / "config.runtime.yaml"
    runtime_path.parent.mkdir()
    runtime_path.write_text(
        "dream:\n  enabled: false\n  base_url: https://runtime.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMBRE_STATE_DIR", str(runtime_path.parent))
    monkeypatch.setenv("OMBRE_DREAM_BASE_URL", "https://env.example")

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["dream"]["enabled"] is False
    assert config["dream"]["base_url"] == "https://env.example"


def test_load_config_dream_enabled_survives_restart_despite_conflicting_env_var(tmp_path, monkeypatch):
    """Real bug (2026-07-17, Yi Lan): the Dashboard's dream on/off toggle
    persists to config.runtime.yaml, but OMBRE_DREAM_ENABLED (which the
    README tells everyone to set in .env during setup) used to win
    unconditionally on every restart, silently reverting a saved "off" back
    to "on" -- the toggle looked like it worked (saved fine, no error) but
    had no lasting effect. A saved choice must survive a restart even with
    the env var still set to the opposite value."""
    runtime_path = tmp_path / "state" / "config.runtime.yaml"
    runtime_path.parent.mkdir()
    runtime_path.write_text("dream:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("OMBRE_STATE_DIR", str(runtime_path.parent))
    monkeypatch.setenv("OMBRE_DREAM_ENABLED", "true")  # the conflicting env var

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["dream"]["enabled"] is False  # the Dashboard's saved choice wins


def test_load_config_dream_env_var_still_bootstraps_a_fresh_install(tmp_path, monkeypatch):
    """The other half of the same fix: before the Dashboard has ever saved a
    preference (no config.runtime.yaml entry for dream.enabled at all), the
    env var must still work as the documented first-run default."""
    monkeypatch.setenv("OMBRE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMBRE_DREAM_ENABLED", "false")

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["dream"]["enabled"] is False


def test_load_config_embedding_reranker_recall_diagnostics_same_fix(tmp_path, monkeypatch):
    """Same restart-reverts-your-choice bug existed for three other toggles
    that follow the identical env-var-bootstrap pattern -- fixed the same way."""
    runtime_path = tmp_path / "state" / "config.runtime.yaml"
    runtime_path.parent.mkdir()
    runtime_path.write_text(
        "embedding:\n  enabled: false\nreranker:\n  enabled: false\n"
        "recall_diagnostics:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMBRE_STATE_DIR", str(runtime_path.parent))
    monkeypatch.setenv("OMBRE_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RERANKER_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RECALL_DIAGNOSTICS_ENABLED", "false")

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["embedding"]["enabled"] is False
    assert config["reranker"]["enabled"] is False
    assert config["recall_diagnostics"]["enabled"] is True


def test_parse_human_date_reference_accepts_common_memory_formats():
    now = datetime(2026, 6, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_human_date_reference("2026.06.15", now=now)["date"] == "2026-06-15"
    assert parse_human_date_reference("2026-06-15", now=now)["date"] == "2026-06-15"
    assert parse_human_date_reference("2026/6/15", now=now)["date"] == "2026-06-15"
    assert parse_human_date_reference("2026年6月15日", now=now)["date"] == "2026-06-15"
    assert parse_human_date_reference("25年6月15日", now=now)["date"] == "2025-06-15"
    assert parse_human_date_reference("6月15日聊了什么", now=now)["date"] == "2026-06-15"
    assert local_date_key("2026.06.15") == "2026-06-15"
    assert local_date_key("2026-06-14T18:30:00+00:00") == "2026-06-15"
    assert strip_human_date_references("2026.06.15聊求职") == " 聊求职"
