import asyncio
import datetime as dt
from unittest import mock

from garmin_mcp import coach, server


def test_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "get_today", "get_plan_overview", "get_planned_week", "get_athlete_snapshot", "get_wellness_trend",
        "list_activities", "analyze_activity", "get_training_status", "review_week", "check_fatigue",
        "analyze_test", "get_progress", "get_race_outlook", "push_workout", "push_custom_workout", "push_week",
    }
    assert expected <= names


def test_plan_tools_via_mcp():
    res = asyncio.run(server.mcp.call_tool("get_planned_week", {"week": 12}))
    assert not res.is_error
    text = res.content[0].text
    assert "TT1" in text


def test_push_workout_preview_and_confirm(fake):
    with mock.patch.object(server, "_client", fake), \
         mock.patch.object(coach, "today", return_value=dt.date(2026, 11, 7)):
        prev = server.push_workout("2026-11-07", confirm=False)
        assert prev["preview"] and not fake.uploaded
        done = server.push_workout("2026-11-07", confirm=True)
        assert done["uploaded"] and fake.scheduled == [(1001, "2026-11-07")]


def test_push_rest_day(fake):
    with mock.patch.object(server, "_client", fake):
        r = server.push_workout("2026-11-06", confirm=True)
        assert "Rest day" in r["message"] and not fake.uploaded


def test_http_token_guard():
    from starlette.testclient import TestClient

    from garmin_mcp.http import build_app

    with mock.patch.dict("os.environ", {"GARMIN_MCP_TOKEN": "s3cret"}):
        app = build_app(server.mcp, "0.0.0.0")
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        hdrs = {"accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}
        r = c.post("/mcp?token=s3cret", json=body, headers=hdrs)
        assert r.status_code != 401
        r2 = c.post("/mcp", json=body, headers={**hdrs, "authorization": "Bearer s3cret"})
        assert r2.status_code != 401
