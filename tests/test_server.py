import json
import logging
from unittest import mock

import pytest
from starlette.testclient import TestClient

import server
from conftest import SECRET, FakeGarmin, MemoryStore
from garmin_client import GarminSession

HDRS = {"accept": "application/json, text/event-stream", "content-type": "application/json",
        "mcp-protocol-version": "2025-06-18"}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


@pytest.fixture
def garmin():
    g = FakeGarmin()
    sess = GarminSession(lambda: MemoryStore(), lambda: g)
    with mock.patch.object(server, "session", sess):
        yield g


@pytest.fixture
def client(garmin, monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    with TestClient(server.create_app(SECRET)) as c:
        yield c


def call_tool(c, name, args=None):
    r = c.post(f"/{SECRET}/mcp", json=rpc("tools/call", {"name": name, "arguments": args or {}}), headers=HDRS)
    assert r.status_code == 200, r.text
    return r.json()["result"]


# ------------------------------------------------------------------ routing

def test_health_does_not_touch_garmin(monkeypatch):
    def explode():
        raise AssertionError("health must not touch Garmin")

    with mock.patch.object(server, "session", GarminSession(explode, explode)):
        with TestClient(server.create_app(SECRET)) as c:
            r = c.get("/health")
    assert r.status_code == 200 and r.text == "ok"


@pytest.mark.parametrize("path", ["/", "/mcp", "/wrong/mcp", f"/{SECRET[:-1]}/mcp", f"/{SECRET}", "/sse", "/health/x"])
def test_other_paths_404(client, path):
    assert client.post(path, json=rpc("tools/list"), headers=HDRS).status_code == 404
    assert client.get(path).status_code == 404


def test_short_secret_refused():
    with pytest.raises(SystemExit):
        server.create_app("short")


def test_access_log_redacts_secret(client, caplog):
    with caplog.at_level(logging.INFO, logger="garmin_mcp"):
        client.post(f"/{SECRET}/mcp", json=rpc("tools/list"), headers=HDRS)
        client.get(f"/{SECRET}xyz")
    assert SECRET not in caplog.text
    assert "/<secret>/mcp" in caplog.text


def test_dns_rebinding_uses_render_host(garmin, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "garmin-mcp-abcd.onrender.com")
    with TestClient(server.create_app(SECRET), base_url="https://garmin-mcp-abcd.onrender.com") as c:
        ok = c.post(f"/{SECRET}/mcp", json=rpc("tools/list"), headers=HDRS)
        assert ok.status_code == 200
        bad = c.post(f"/{SECRET}/mcp", json=rpc("tools/list"), headers={**HDRS, "host": "evil.example.com"})
        assert bad.status_code in (400, 421)


# -------------------------------------------------------------------- tools

def test_tools_list(client):
    r = client.post(f"/{SECRET}/mcp", json=rpc("tools/list"), headers=HDRS)
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {
        "list_activities", "get_activity", "get_daily_summary", "get_sleep", "get_hrv",
        "get_training_readiness", "get_training_status", "get_max_metrics", "get_race_predictions",
        "get_personal_records", "get_endurance_score", "get_lactate_threshold", "get_body_battery",
        "get_heart_rates",
    }
    for t in tools.values():
        assert len(t["description"]) > 40
        assert t["annotations"]["readOnlyHint"] is True


def test_list_activities_trimmed(client, garmin):
    res = call_tool(client, "list_activities", {"start_date": "2026-11-01", "end_date": "2026-11-07"})
    assert not res["isError"]
    data = res["structuredContent"]
    first = data["activities"][0]
    assert first["id"] == 102  # newest first
    second = data["activities"][1]
    assert second == {
        "id": 101, "name": "Morning Run", "type": "running", "start_local": "2026-11-05 05:12:00",
        "distance_km": 7.01, "duration": "45:00", "moving_time": "44:40", "avg_pace_min_km": "6:25",
        "avg_hr": 146, "max_hr": 158, "elevation_gain_m": 21, "cadence_spm": 168, "aerobic_te": 2.8,
        "anaerobic_te": 0.2, "vo2max": 44.0,
    }
    lim = call_tool(client, "list_activities", {"limit": 1})["structuredContent"]
    assert lim["count"] == 1


def test_get_activity(client):
    d = call_tool(client, "get_activity", {"activity_id": "102"})["structuredContent"]
    assert d["summary"]["avg_pace_min_km"] == "6:47"
    assert len(d["laps"]) == 3 and d["laps"][0]["pace_min_km"] == "6:41"
    assert d["hr_zones"][4] == {"zone": 5, "from_bpm": 175, "time": "50:00", "minutes": 50.0}


def test_wellness_tools(client):
    s = call_tool(client, "get_sleep", {"date": "2026-11-07"})["structuredContent"]
    assert s["hours"] == 7.0 and s["score"] == 81 and s["stages_min"]["deep"] == 90
    assert "sleepMovement" not in json.dumps(s)
    d = call_tool(client, "get_daily_summary", {"date": "2026-11-07"})["structuredContent"]
    assert d["steps"] == 12034 and d["body_battery"] == {"high": 88, "low": 21}
    h = call_tool(client, "get_hrv", {"date": "2026-11-07"})["structuredContent"]
    assert h["status"] == "BALANCED" and "hrvReadings" not in json.dumps(h)
    tr = call_tool(client, "get_training_readiness")["structuredContent"]
    assert tr["readings"][0]["recovery_time_h"] == 6.0
    ts = call_tool(client, "get_training_status")["structuredContent"]
    assert ts["vo2max"] == 44.6 and ts["load"]["acwr"] == 1.1


def test_fitness_tools(client):
    assert call_tool(client, "get_max_metrics")["structuredContent"]["vo2max_running"] == 44.6
    rp = call_tool(client, "get_race_predictions")["structuredContent"]
    assert rp["half_marathon"] == "2:07:00"
    pr = call_tool(client, "get_personal_records")["structuredContent"]
    assert [r["record"] for r in pr["running"]] == ["5 km", "10 km", "longest run"]
    assert pr["running"][1]["time"] == "1:00:01" and pr["running"][2]["distance_km"] == 21.1
    es = call_tool(client, "get_endurance_score")["structuredContent"]
    assert es["score"] == 5421
    lt = call_tool(client, "get_lactate_threshold")["structuredContent"]
    assert lt["heart_rate"] == 168 and lt["pace_min_km"] == "5:27"


def test_body_battery_and_hr(client):
    bb = call_tool(client, "get_body_battery", {"start_date": "2026-11-06", "end_date": "2026-11-07"})["structuredContent"]
    assert bb["days"][0] == {"date": "2026-11-06", "charged": 60, "drained": 55, "high": 85, "low": 30,
                             "start_level": 30, "end_level": 40}
    hr = call_tool(client, "get_heart_rates", {"date": "2026-11-07"})["structuredContent"]
    assert hr["resting_hr"] == 52 and len(hr["hourly"]) == 2
    assert hr["hourly"][0]["hour"] == "00:00"


def test_bad_input_is_clean_error(client):
    res = call_tool(client, "get_sleep", {"date": "07/11/2026"})
    assert res["isError"] and "YYYY-MM-DD" in res["content"][0]["text"]
    res = call_tool(client, "get_activity", {"activity_id": "abc"})
    assert res["isError"]


def test_auth_error_is_clean_message(monkeypatch):
    sess = GarminSession(lambda: MemoryStore(), lambda: FakeGarmin(auth_fail=True))
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    with mock.patch.object(server, "session", sess), TestClient(server.create_app(SECRET)) as c:
        res = call_tool(c, "get_race_predictions")
    text = res["content"][0]["text"]
    assert res["isError"] and "setup_tokens.py --rotate" in text and "Traceback" not in text
