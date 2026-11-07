"""Build Garmin Connect structured running workouts from plan session strings."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .plan import Plan, Repeat, Segment, Step, describe_session, parse_session

_SPORT_RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}

_STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}

_COND_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_COND_DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
_COND_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}

_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_TARGET_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 1}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 1}

_ZONE_LABEL = {
    "recovery": "Recovery",
    "easy": "Easy",
    "aerobic_test": "Aerobic test",
    "steady": "Steady",
    "tempo": "Tempo",
    "tempo_test": "Tempo test",
    "hmp": "HM pace",
    "tt": "Time trial",
    "race": "Race",
    "strides": "Strides",
}


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _target(plan: Plan, zone: str) -> dict[str, Any]:
    lo, hi = plan.hr_range(zone)
    if zone == "recovery":
        lo, hi = 100, hi or 137
    if lo is not None and hi is not None:
        return {"targetType": _TARGET_HR, "targetValueOne": float(lo), "targetValueTwo": float(hi)}
    if zone in ("hmp", "race") and plan.race_pace_s_per_km:
        # Garmin pace targets are in m/s; the slower pace is the lower bound. ±5 s/km window.
        p = plan.race_pace_s_per_km
        return {
            "targetType": _TARGET_PACE,
            "targetValueOne": round(1000.0 / (p + 5), 4),
            "targetValueTwo": round(1000.0 / (p - 5), 4),
        }
    return {"targetType": _TARGET_NONE}


def _exec_step(plan: Plan, st: Step, order: _Counter, step_type: str) -> dict[str, Any]:
    if st.km is not None:
        cond, value = _COND_DIST, round(st.km * 1000.0, 1)
    else:
        cond, value = _COND_TIME, float(st.seconds or 0)
    out = {
        "type": "ExecutableStepDTO",
        "stepOrder": order.next(),
        "stepType": _STEP_TYPES[step_type],
        "endCondition": cond,
        "endConditionValue": value,
        "description": f"{_ZONE_LABEL.get(st.zone, st.zone)} – {st.describe()}",
        **_target(plan, st.zone),
    }
    return out


def _step_role(segments: list[Segment], i: int, st: Step) -> str:
    """Easy segments at the very start/end become warmup/cooldown; everything else is an interval."""
    easyish = st.zone in ("easy", "recovery")
    if easyish and len(segments) > 1 and i == 0:
        return "warmup"
    if easyish and len(segments) > 1 and i == len(segments) - 1:
        return "cooldown"
    return "interval"


def build_workout(plan: Plan, session: str, name: str, day: dt.date | None = None,
                  description: str | None = None) -> dict[str, Any]:
    """Return a Garmin workout JSON dict for `session` (plan mini-language)."""
    segments = parse_session(session)
    if not segments:
        raise ValueError("Session is a rest day – nothing to build")
    order = _Counter()
    steps: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        if isinstance(seg, Repeat):
            group_order = order.next()
            children = [_exec_step(plan, seg.work, order, "interval")]
            if seg.recovery:
                children.append(_exec_step(plan, seg.recovery, order, "recovery"))
            steps.append({
                "type": "RepeatGroupDTO",
                "stepOrder": group_order,
                "stepType": _STEP_TYPES["repeat"],
                "numberOfIterations": seg.times,
                "endCondition": _COND_ITER,
                "endConditionValue": float(seg.times),
                "smartRepeat": False,
                "skipLastRestStep": True,
                "workoutSteps": children,
            })
        else:
            steps.append(_exec_step(plan, seg, order, _step_role(segments, i, seg)))

    day = day or dt.date.today()
    est = int(plan.estimate_seconds(segments, day))
    return {
        "workoutName": name[:80],
        "description": (description or describe_session(segments))[:1000],
        "sportType": _SPORT_RUNNING,
        "estimatedDurationInSecs": est,
        "workoutSegments": [{"segmentOrder": 1, "sportType": _SPORT_RUNNING, "workoutSteps": steps}],
    }


def workout_name_for(day_plan: dict[str, Any]) -> str:
    d = dt.date.fromisoformat(day_plan["date"])
    label = {
        "key_run": (day_plan.get("key_type") or "key").replace("_", " ").upper(),
        "weekday_easy": "Easy",
        "weekend_easy": "Easy long" if (day_plan.get("planned_km") or 0) >= 10 else "Easy",
        "test": "Test",
    }.get(day_plan.get("role", ""), "Run")
    if day_plan.get("test"):
        label = day_plan["test"]["id"]
    return f"HM27 W{day_plan['week']:02d} {d.strftime('%a %d %b')} · {label} {day_plan['planned_km']:g}k"
