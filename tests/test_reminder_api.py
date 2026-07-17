import json

import pytest

from reminder_store import ReminderStore


class DummyRequest:
    def __init__(self, body=None, headers=None, cookies=None, path_params=None, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.path_params = path_params or {}
        self.query_params = query_params or {}

    async def json(self):
        return self._body


def _reminder_store(tmp_path) -> ReminderStore:
    return ReminderStore({"state_dir": str(tmp_path / "state"), "buckets_dir": str(tmp_path / "buckets")})


@pytest.mark.asyncio
async def test_reminder_create_and_list_mcp_tools(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))

    created = await server.reminder_create(title="喂猫", content="记得晚上喂猫粮")
    listed = await server.reminder_list(status="active")

    assert created["status"] == "created"
    assert created["reminder"]["title"] == "喂猫"
    assert listed["count"] == 1
    assert listed["reminders"][0]["id"] == created["reminder"]["id"]


@pytest.mark.asyncio
async def test_reminder_create_mcp_tool_rejects_missing_content(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))

    result = await server.reminder_create(title="喂猫", content="")

    assert "error" in result


@pytest.mark.asyncio
async def test_reminder_update_mcp_tool_snoozes_and_marks_done(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))
    created = await server.reminder_create(title="喂猫", content="记得晚上喂猫粮")
    reminder_id = created["reminder"]["id"]

    snoozed = await server.reminder_update(reminder_id, snooze_minutes=30)
    done = await server.reminder_update(reminder_id, status="done")

    assert snoozed["status"] == "updated"
    assert snoozed["reminder"]["status"] == "active"
    assert done["reminder"]["status"] == "done"


@pytest.mark.asyncio
async def test_reminder_update_mcp_tool_missing_id_returns_error(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))

    result = await server.reminder_update("", status="done")

    assert "error" in result


@pytest.mark.asyncio
async def test_api_reminders_rest_endpoints_create_list_update(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    create_response = await server.api_reminder_create(
        DummyRequest({"title": "喂猫", "content": "记得晚上喂猫粮", "repeat_rule": "once"})
    )
    create_payload = json.loads(create_response.body)
    reminder_id = create_payload["reminder"]["id"]

    list_response = await server.api_reminders(DummyRequest(query_params={"status": "active"}))
    list_payload = json.loads(list_response.body)

    update_response = await server.api_reminder_update(
        DummyRequest({"status": "done"}, path_params={"reminder_id": reminder_id}),
    )
    update_payload = json.loads(update_response.body)

    assert create_response.status_code == 200
    assert list_payload["count"] == 1
    assert update_response.status_code == 200
    assert update_payload["reminder"]["status"] == "done"


@pytest.mark.asyncio
async def test_api_reminder_create_rejects_missing_title(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    response = await server.api_reminder_create(DummyRequest({"title": "", "content": "内容"}))

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_api_reminder_update_not_found_returns_404(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "reminder_store", _reminder_store(tmp_path))
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    response = await server.api_reminder_update(
        DummyRequest({"status": "done"}, path_params={"reminder_id": "does-not-exist"})
    )

    assert response.status_code == 404
