"""Shared fixtures: a fake Garmin client that serves synthetic but realistic data."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from garmin_mcp.plan import load_plan


def make_details(segments: list[tuple[float, float, float]], burst_every: float | None = None) -> dict[str, Any]:
    """segments: list of (duration_s, speed_mps, hr). 1 Hz samples."""
    rows = []
    t = dist = 0.0
    for dur, speed, hr in segments:
        for _ in range(int(dur)):
            rows.append({"metrics": [t, dist, hr, speed]})
            t += 1
            dist += speed
    return {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "sumDuration"},
            {"metricsIndex": 1, "key": "sumDistance"},
            {"metricsIndex": 2, "key": "directHeartRate"},
            {"metricsIndex": 3, "key": "directSpeed"},
        ],
        "activityDetailMetrics": rows,
    }


def activity(aid: int, date: str, segments: list[tuple[float, float, float]], type_key: str = "running",
             time: str = "05:10:00") -> tuple[dict[str, Any], dict[str, Any]]:
    det = make_details(segments)
    dur = sum(s[0] for s in segments)
    dist = sum(s[0] * s[1] for s in segments)
    hr = sum(s[0] * s[2] for s in segments) / dur if dur else 0
    summary = {
        "activityId": aid,
        "activityName": f"Run {aid}",
        "startTimeLocal": f"{date} {time}",
        "activityType": {"typeKey": type_key},
        "distance": dist,
        "duration": dur,
        "movingDuration": dur,
        "averageSpeed": dist / dur if dur else 0,
        "averageHR": hr,
        "maxHR": max((s[2] for s in segments), default=0),
    }
    return summary, det


class FakeClient:
    def __init__(self) -> None:
        self.acts: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.sleep: dict[str, float] = {}
        self.uploaded: list[dict[str, Any]] = []
        self.scheduled: list[tuple[Any, str]] = []

    def add(self, summary: dict[str, Any], details: dict[str, Any]) -> None:
        self.acts[summary["activityId"]] = (summary, details)

    # mimic GarminClient.call / safe
    def call(self, method: str, *args: Any, cache: bool = True, **kw: Any) -> Any:
        return getattr(self, "_" + method)(*args, **kw)

    def safe(self, method: str, *args: Any, **kw: Any) -> Any:
        fn = getattr(self, "_" + method, None)
        return fn(*args, **kw) if fn else None

    def _get_activities_by_date(self, start: str, end: str | None = None, *_: Any) -> list[dict[str, Any]]:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end or start)
        return [a for a, _ in self.acts.values()
                if s <= dt.date.fromisoformat(a["startTimeLocal"][:10]) <= e]

    def _get_activity(self, aid: str) -> dict[str, Any]:
        return self.acts[int(aid)][0]

    def _get_activity_details(self, aid: str) -> dict[str, Any]:
        return self.acts[int(aid)][1]

    def _get_activity_splits(self, aid: str) -> dict[str, Any]:
        return {"lapDTOs": []}

    def _get_sleep_data(self, d: str) -> dict[str, Any]:
        h = self.sleep.get(d)
        if h is None:
            return {}
        return {"dailySleepDTO": {"calendarDate": d, "sleepTimeSeconds": h * 3600,
                                  "sleepScores": {"overall": {"value": 80 if h >= 7 else 55}}},
                "restingHeartRate": 52}

    def _get_hrv_data(self, d: str) -> dict[str, Any]:
        return {"hrvSummary": {"lastNightAvg": 60, "weeklyAvg": 58, "status": "BALANCED"}}

    def _get_training_readiness(self, d: str) -> list[dict[str, Any]]:
        return [{"score": 70, "level": "HIGH"}]

    def _get_training_status(self, d: str) -> dict[str, Any]:
        return {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 45.2, "calendarDate": d}}}

    def _get_user_summary(self, d: str) -> dict[str, Any]:
        return {"restingHeartRate": 52, "bodyBatteryMostRecentValue": 70}

    def _get_race_predictions(self, *_: Any) -> dict[str, Any]:
        return {"time5K": 1650, "time10K": 3480, "timeHalfMarathon": 7800, "timeMarathon": 16800}

    def _upload_workout(self, wk: dict[str, Any]) -> dict[str, Any]:
        self.uploaded.append(wk)
        return {"workoutId": 1000 + len(self.uploaded)}

    def _schedule_workout(self, wid: Any, d: str) -> dict[str, Any]:
        self.scheduled.append((wid, d))
        return {}


@pytest.fixture
def plan():
    return load_plan()


@pytest.fixture
def fake():
    return FakeClient()
