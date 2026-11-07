"""Personal, single-user remote MCP server exposing my Garmin Connect data to Claude.

Endpoint: /{MCP_PATH_SECRET}/mcp  (streamable HTTP, stateless, JSON responses)
Health:   GET /health -> "ok" (never touches Garmin)
Anything else -> 404.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import statistics
from typing import Any, Callable
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from garmin_client import GarminAuthError, GarminCallError, GarminSession

log = logging.getLogger("garmin_mcp")
TZ = ZoneInfo("Asia/Jakarta")

INSTRUCTIONS = """\
Read-only access to the owner's own Garmin Connect account (running-focused).
Dates are YYYY-MM-DD and default to today in Asia/Jakarta. Paces are min/km, distances km,
durations h:mm:ss. Typical flows: recent training → list_activities then get_activity for details;
recovery/readiness → get_sleep, get_hrv, get_training_readiness, get_daily_summary;
fitness trend → get_training_status, get_max_metrics, get_race_predictions, get_endurance_score.
If a tool returns an authentication error, tell the user to run `python scripts/setup_tokens.py --rotate`.
"""

session = GarminSession()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def _date(value: str | None) -> str:
    if not value or value.strip().lower() == "today":
        return today().isoformat()
    if value.strip().lower() == "yesterday":
        return (today() - dt.timedelta(days=1)).isoformat()
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ToolError(f"Invalid date {value!r}: use YYYY-MM-DD.") from None


def _or_msg(d: dict[str, Any], msg: str) -> dict[str, Any]:
    """Return d, or a 'no data' message if it holds nothing beyond the date."""
    return d if len(d) > 1 else {**d, "message": msg}


def _clean(d: Any) -> Any:
    """Drop None / empty values recursively so responses stay small."""
    if isinstance(d, dict):
        out = {k: _clean(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(d, list):
        return [_clean(x) for x in d if x not in (None, "", [], {})]
    return d


def _r(x: Any, nd: int = 1) -> float | int | None:
    if x is None:
        return None
    try:
        v = round(float(x), nd)
    except (TypeError, ValueError):
        return None
    return int(v) if nd == 0 else v


def hms(seconds: Any) -> str | None:
    if seconds is None:
        return None
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def pace(speed_mps: Any) -> str | None:
    """m/s -> 'm:ss' per km."""
    try:
        v = float(speed_mps)
    except (TypeError, ValueError):
        return None
    if v <= 0.3:
        return None
    s = int(round(1000 / v))
    return f"{s // 60}:{s % 60:02d}"


def km(meters: Any) -> float | None:
    return _r(float(meters) / 1000, 2) if meters is not None else None


def _local_clock(ms: Any) -> str | None:
    """Garmin '*TimestampLocal' values are epoch-ms already shifted to local time."""
    if not ms:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ms) / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _run(fn: Callable[[Any], Any]) -> Any:
    """Execute against Garmin; failures become a clear tool error (no stack trace)."""
    try:
        return session.call(fn)
    except (GarminAuthError, GarminCallError) as e:
        raise ToolError(str(e)) from None


# --------------------------------------------------------------------------- #
# Formatters (trim Garmin's JSON)
# --------------------------------------------------------------------------- #

def fmt_activity(a: dict[str, Any]) -> dict[str, Any]:
    dur = a.get("duration")
    return _clean({
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": (a.get("activityType") or {}).get("typeKey"),
        "start_local": a.get("startTimeLocal"),
        "distance_km": km(a.get("distance")),
        "duration": hms(dur),
        "moving_time": hms(a.get("movingDuration")),
        "avg_pace_min_km": pace(a.get("averageSpeed")),
        "avg_hr": _r(a.get("averageHR"), 0),
        "max_hr": _r(a.get("maxHR"), 0),
        "elevation_gain_m": _r(a.get("elevationGain"), 0),
        "cadence_spm": _r(a.get("averageRunningCadenceInStepsPerMinute"), 0),
        "aerobic_te": _r(a.get("aerobicTrainingEffect")),
        "anaerobic_te": _r(a.get("anaerobicTrainingEffect")),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": _r(a.get("activityTrainingLoad"), 0),
        "vo2max": a.get("vO2MaxValue"),
        "calories": _r(a.get("calories"), 0),
    })


def fmt_activity_detail(a: dict[str, Any]) -> dict[str, Any]:
    s = a.get("summaryDTO") or {}
    return _clean({
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": (a.get("activityTypeDTO") or {}).get("typeKey"),
        "start_local": s.get("startTimeLocal"),
        "location": a.get("locationName"),
        "distance_km": km(s.get("distance")),
        "duration": hms(s.get("duration")),
        "moving_time": hms(s.get("movingDuration")),
        "elapsed_time": hms(s.get("elapsedDuration")),
        "avg_pace_min_km": pace(s.get("averageSpeed")),
        "avg_moving_pace_min_km": pace(s.get("averageMovingSpeed")),
        "avg_hr": _r(s.get("averageHR"), 0),
        "max_hr": _r(s.get("maxHR"), 0),
        "min_hr": _r(s.get("minHR"), 0),
        "cadence_spm": _r(s.get("averageRunCadence"), 0),
        "max_cadence_spm": _r(s.get("maxRunCadence"), 0),
        "stride_length_m": _r((s.get("strideLength") or 0) / 100, 2) if s.get("strideLength") else None,
        "ground_contact_ms": _r(s.get("groundContactTime"), 0),
        "vertical_oscillation_cm": _r(s.get("verticalOscillation"), 1),
        "avg_power_w": _r(s.get("averagePower"), 0),
        "elevation_gain_m": _r(s.get("elevationGain"), 0),
        "elevation_loss_m": _r(s.get("elevationLoss"), 0),
        "aerobic_te": _r(s.get("trainingEffect") or s.get("aerobicTrainingEffect")),
        "anaerobic_te": _r(s.get("anaerobicTrainingEffect")),
        "te_label": s.get("trainingEffectLabel"),
        "training_load": _r(s.get("activityTrainingLoad"), 0),
        "calories": _r(s.get("calories"), 0),
        "avg_temp_c": _r(s.get("averageTemperature"), 0),
        "recovery_hr": _r(s.get("recoveryHeartRate"), 0),
    })


def fmt_laps(splits: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for lap in (splits or {}).get("lapDTOs") or []:
        out.append(_clean({
            "lap": lap.get("lapIndex"),
            "distance_km": km(lap.get("distance")),
            "time": hms(lap.get("duration")),
            "pace_min_km": pace(lap.get("averageSpeed")),
            "avg_hr": _r(lap.get("averageHR"), 0),
            "max_hr": _r(lap.get("maxHR"), 0),
            "cadence_spm": _r(lap.get("averageRunCadence"), 0),
            "elev_gain_m": _r(lap.get("elevationGain"), 0),
            "elev_loss_m": _r(lap.get("elevationLoss"), 0),
        }))
    return out


def fmt_hr_zones(z: Any) -> list[dict[str, Any]]:
    out = []
    for item in z or []:
        if not isinstance(item, dict):
            continue
        out.append(_clean({
            "zone": item.get("zoneNumber"),
            "from_bpm": item.get("zoneLowBoundary"),
            "time": hms(item.get("secsInZone")),
            "minutes": _r((item.get("secsInZone") or 0) / 60, 1),
        }))
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def list_activities(start_date: str | None = None, end_date: str | None = None,
                    activity_type: str = "running", limit: int = 30) -> dict[str, Any]:
    """List my Garmin activities in a date range (newest first).

    Use this to see recent training: what I ran, how far, how fast, at what heart rate.
    Each item has id (pass it to get_activity), name, type, local start time, distance (km),
    duration, avg pace (min/km), avg/max HR, elevation gain, cadence, training effect, VO2max.

    Args:
        start_date: YYYY-MM-DD, default 7 days before end_date.
        end_date: YYYY-MM-DD, default today (Asia/Jakarta).
        activity_type: Garmin type key, e.g. "running", "trail_running", "cycling",
            "strength_training", or "all" for every type. Default "running".
        limit: Max activities returned (1–200), default 30.
    """
    end = _date(end_date)
    start = _date(start_date) if start_date else (dt.date.fromisoformat(end) - dt.timedelta(days=7)).isoformat()
    if start > end:
        raise ToolError("start_date is after end_date")
    atype = None if (activity_type or "all").lower() in ("all", "any", "") else activity_type
    limit = max(1, min(int(limit), 200))

    def fn(g: Any) -> Any:
        acts = g.get_activities_by_date(start, end, atype) or []
        acts.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)
        return {"start_date": start, "end_date": end, "count": len(acts[:limit]),
                "activities": [fmt_activity(a) for a in acts[:limit]]}

    return _run(fn)


def get_activity(activity_id: str) -> dict[str, Any]:
    """Full detail for one activity: summary, per-lap splits (usually per km with auto-lap:
    pace, HR, cadence, elevation) and time in each heart-rate zone.

    Use after list_activities when I ask how a specific run went, about splits, pacing,
    or HR distribution.

    Args:
        activity_id: The numeric activity id from list_activities.
    """
    aid = str(activity_id).strip()
    if not aid.isdigit():
        raise ToolError("activity_id must be the numeric id from list_activities")

    def fn(g: Any) -> Any:
        a = g.get_activity(aid)
        out = {"summary": fmt_activity_detail(a)}
        try:
            out["laps"] = fmt_laps(g.get_activity_splits(aid))
        except Exception as e:  # noqa: BLE001
            if "Authentication" in type(e).__name__:
                raise
            out["laps_error"] = type(e).__name__
        try:
            out["hr_zones"] = fmt_hr_zones(g.get_activity_hr_in_timezones(aid))
        except Exception as e:  # noqa: BLE001
            if "Authentication" in type(e).__name__:
                raise
            out["hr_zones_error"] = type(e).__name__
        return _clean(out)

    return _run(fn)


def get_daily_summary(date: str | None = None) -> dict[str, Any]:
    """Daily wellness totals: steps, distance, resting HR (and 7-day avg), min/max HR, stress,
    Body Battery high/low/current, calories, intensity minutes, floors.

    Use for "how was my day", recovery context, or general activity level.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        s = g.get_user_summary(d) or {}
        return _clean({
            "date": d,
            "steps": s.get("totalSteps"),
            "step_goal": s.get("dailyStepGoal"),
            "distance_km": km(s.get("totalDistanceMeters")),
            "resting_hr": s.get("restingHeartRate"),
            "resting_hr_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate"),
            "min_hr": s.get("minHeartRate"),
            "max_hr": s.get("maxHeartRate"),
            "stress_avg": s.get("averageStressLevel"),
            "stress_max": s.get("maxStressLevel"),
            "stress_qualifier": s.get("stressQualifier"),
            "body_battery": {
                "high": s.get("bodyBatteryHighestValue"),
                "low": s.get("bodyBatteryLowestValue"),
                "current": s.get("bodyBatteryMostRecentValue"),
                "at_wake": s.get("bodyBatteryAtWakeTime"),
                "charged": s.get("bodyBatteryChargedValue"),
                "drained": s.get("bodyBatteryDrainedValue"),
            },
            "calories": {
                "total": _r(s.get("totalKilocalories"), 0),
                "active": _r(s.get("activeKilocalories"), 0),
                "bmr": _r(s.get("bmrKilocalories"), 0),
            },
            "intensity_minutes": {
                "moderate": s.get("moderateIntensityMinutes"),
                "vigorous": s.get("vigorousIntensityMinutes"),
                "weekly_goal": s.get("intensityMinutesGoal"),
            },
            "floors_climbed": _r(s.get("floorsAscended"), 0),
        })

    return _run(fn)


def get_sleep(date: str | None = None) -> dict[str, Any]:
    """Sleep for the night ending on `date` (the morning you woke up): duration, stages
    (deep/light/REM/awake), sleep score and qualifier, bed/wake times, overnight HRV,
    resting HR, respiration, Body Battery gained.

    Use for recovery questions ("did I sleep enough?", "am I recovered for today's run?").

    Args:
        date: YYYY-MM-DD of the wake-up day, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        raw = g.get_sleep_data(d) or {}
        s = raw.get("dailySleepDTO") or {}
        scores = s.get("sleepScores") or {}

        def mins(key: str) -> int | None:
            return _r((s.get(key) or 0) / 60, 0) if s.get(key) is not None else None

        return _or_msg(_clean({
            "date": d,
            "duration": hms(s.get("sleepTimeSeconds")),
            "hours": _r((s.get("sleepTimeSeconds") or 0) / 3600, 2) if s.get("sleepTimeSeconds") else None,
            "bed_time_local": _local_clock(s.get("sleepStartTimestampLocal")),
            "wake_time_local": _local_clock(s.get("sleepEndTimestampLocal")),
            "stages_min": {"deep": mins("deepSleepSeconds"), "light": mins("lightSleepSeconds"),
                           "rem": mins("remSleepSeconds"), "awake": mins("awakeSleepSeconds")},
            "score": (scores.get("overall") or {}).get("value"),
            "quality": (scores.get("overall") or {}).get("qualifierKey"),
            "feedback": s.get("sleepScoreFeedback"),
            "avg_overnight_hrv": raw.get("avgOvernightHrv"),
            "hrv_status": raw.get("hrvStatus"),
            "resting_hr": raw.get("restingHeartRate"),
            "avg_respiration": s.get("averageRespirationValue"),
            "avg_sleep_stress": s.get("avgSleepStress"),
            "body_battery_change": raw.get("bodyBatteryChange"),
            "awake_count": s.get("awakeCount"),
        }), "No sleep recorded for this date.")

    return _run(fn)


def get_hrv(date: str | None = None) -> dict[str, Any]:
    """Heart-rate variability status for the night ending on `date`: last-night average,
    7-day average, personal baseline range and status (BALANCED / UNBALANCED / LOW).

    Use as a recovery/strain signal before hard sessions.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        h = (g.get_hrv_data(d) or {}).get("hrvSummary") or {}
        b = h.get("baseline") or {}
        return _or_msg(_clean({
            "date": h.get("calendarDate") or d,
            "last_night_avg_ms": h.get("lastNightAvg"),
            "last_night_5min_high_ms": h.get("lastNight5MinHigh"),
            "weekly_avg_ms": h.get("weeklyAvg"),
            "baseline": {"low_upper": b.get("lowUpper"), "balanced_low": b.get("balancedLow"),
                         "balanced_upper": b.get("balancedUpper")},
            "status": h.get("status"),
            "feedback": h.get("feedbackPhrase"),
        }), "No HRV data for this date.")

    return _run(fn)


def get_training_readiness(date: str | None = None) -> dict[str, Any]:
    """Garmin Training Readiness (0–100) with its contributing factors: sleep, recovery time,
    acute load (ACWR), HRV, stress and sleep history.

    Use to decide whether today suits a hard/key session or should be easy.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        items = g.get_training_readiness(d) or []
        if isinstance(items, dict):
            items = [items]
        out = []
        for t in items:
            out.append(_clean({
                "time_local": t.get("timestampLocal") or t.get("timestamp"),
                "score": t.get("score"),
                "level": t.get("level"),
                "feedback": t.get("feedbackShort"),
                "recovery_time_h": _r((t.get("recoveryTime") or 0) / 60, 1) if t.get("recoveryTime") is not None else None,
                "factors_pct": {
                    "sleep": t.get("sleepScoreFactorPercent"),
                    "recovery_time": t.get("recoveryTimeFactorPercent"),
                    "acute_load": t.get("acwrFactorPercent"),
                    "hrv": t.get("hrvFactorPercent"),
                    "stress_history": t.get("stressHistoryFactorPercent"),
                    "sleep_history": t.get("sleepHistoryFactorPercent"),
                },
                "sleep_score": t.get("sleepScore"),
                "acute_load": t.get("acuteLoad"),
                "hrv_weekly_avg": t.get("hrvWeeklyAverage"),
            }))
        return {"date": d, "readings": out} if out else {"date": d, "message": "No training readiness for this date."}

    return _run(fn)


def get_training_status(date: str | None = None) -> dict[str, Any]:
    """Garmin Training Status (Productive / Maintaining / Unproductive / Overreaching / …),
    VO2max, acute vs chronic training load and ACWR, and monthly load balance
    (low aerobic / high aerobic / anaerobic).

    Use for "am I training too much/too little?" and fitness trend questions.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        ts = g.get_training_status(d) or {}
        out: dict[str, Any] = {"date": d}
        vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic")) or {}
        out["vo2max"] = vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")
        out["vo2max_date"] = vo2.get("calendarDate")
        latest = ((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")) or {}
        for dev in latest.values():
            if not isinstance(dev, dict):
                continue
            acute = dev.get("acuteTrainingLoadDTO") or {}
            out["status"] = dev.get("trainingStatusFeedbackPhrase") or dev.get("trainingStatus")
            out["load"] = {
                "acute": acute.get("dailyTrainingLoadAcute"),
                "chronic": acute.get("dailyTrainingLoadChronic"),
                "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
                "acwr_status": acute.get("acwrStatus"),
                "optimal_chronic_min": acute.get("minTrainingLoadChronic"),
                "optimal_chronic_max": acute.get("maxTrainingLoadChronic"),
            }
            break
        bal = ((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap")) or {}
        for dev in bal.values():
            if isinstance(dev, dict):
                out["monthly_load_balance"] = {
                    "low_aerobic": _r(dev.get("monthlyLoadAerobicLow"), 0),
                    "low_aerobic_target": [_r(dev.get("monthlyLoadAerobicLowTargetMin"), 0), _r(dev.get("monthlyLoadAerobicLowTargetMax"), 0)],
                    "high_aerobic": _r(dev.get("monthlyLoadAerobicHigh"), 0),
                    "high_aerobic_target": [_r(dev.get("monthlyLoadAerobicHighTargetMin"), 0), _r(dev.get("monthlyLoadAerobicHighTargetMax"), 0)],
                    "anaerobic": _r(dev.get("monthlyLoadAnaerobic"), 0),
                    "anaerobic_target": [_r(dev.get("monthlyLoadAnaerobicTargetMin"), 0), _r(dev.get("monthlyLoadAnaerobicTargetMax"), 0)],
                    "feedback": dev.get("trainingBalanceFeedbackPhrase"),
                }
                break
        return _clean(out)

    return _run(fn)


def get_max_metrics(date: str | None = None) -> dict[str, Any]:
    """VO2max (running, and cycling if present) and fitness age for a date.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        raw = g.get_max_metrics(d)
        items = raw if isinstance(raw, list) else [raw] if raw else []
        for m in items:
            gen = (m or {}).get("generic") or {}
            cyc = (m or {}).get("cycling") or {}
            res = _clean({
                "date": gen.get("calendarDate") or d,
                "vo2max_running": gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue"),
                "fitness_age": gen.get("fitnessAge"),
                "vo2max_cycling": cyc.get("vo2MaxPreciseValue") or cyc.get("vo2MaxValue"),
            })
            if len(res) > 1:
                return res
        return {"date": d, "message": "No VO2max for this date (Garmin only records it after qualifying outdoor runs)."}

    return _run(fn)


def get_race_predictions() -> dict[str, Any]:
    """Garmin's current race-time predictions for 5K, 10K, half marathon and marathon."""

    def fn(g: Any) -> Any:
        rp = g.get_race_predictions() or {}
        if isinstance(rp, list):
            rp = rp[-1] if rp else {}
        return _clean({
            "date": rp.get("calendarDate"),
            "5k": hms(rp.get("time5K")),
            "10k": hms(rp.get("time10K")),
            "half_marathon": hms(rp.get("timeHalfMarathon")),
            "marathon": hms(rp.get("timeMarathon")),
        })

    return _run(fn)


_PR_TYPES = {1: "1 km", 2: "1 mile", 3: "5 km", 4: "10 km", 5: "half marathon", 6: "marathon", 7: "longest run"}


def get_personal_records() -> dict[str, Any]:
    """My running personal records (fastest 1K, mile, 5K, 10K, half, marathon, longest run)
    with the date and activity id of each. Other record types are listed with their raw value."""

    def fn(g: Any) -> Any:
        raw = g.get_personal_record() or []
        items = raw if isinstance(raw, list) else raw.get("personalRecords", []) if isinstance(raw, dict) else []
        running, other = [], []
        for pr in items:
            tid = pr.get("typeId")
            base = {
                "date": (pr.get("actStartDateTimeInGMTFormatted") or pr.get("prStartTimeGmtFormatted") or "")[:10] or None,
                "activity_id": pr.get("activityId"),
                "activity_name": pr.get("activityName"),
            }
            if tid in _PR_TYPES:
                val = pr.get("value")
                running.append(_clean({"record": _PR_TYPES[tid],
                                       **({"distance_km": km(val)} if tid == 7 else {"time": hms(val)}),
                                       **base}))
            else:
                other.append(_clean({"type_id": tid, "value": pr.get("value"), **base}))
        running.sort(key=lambda r: list(_PR_TYPES.values()).index(r["record"]))
        return _clean({"running": running, "other": other})

    return _run(fn)


def get_endurance_score(date: str | None = None) -> dict[str, Any]:
    """Garmin Endurance Score for a day (overall score, classification and the contributing
    activity types).

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        es = g.get_endurance_score(d) or {}
        dto = es.get("enduranceScoreDTO") if isinstance(es.get("enduranceScoreDTO"), dict) else es
        contributors = []
        for c in dto.get("contributors") or []:
            contributors.append(_clean({"activity_type_id": c.get("activityTypeId"),
                                        "group": c.get("group"), "share_pct": _r(c.get("contribution"), 1)}))
        return _or_msg(_clean({
            "date": dto.get("calendarDate") or d,
            "score": dto.get("overallScore"),
            "classification": dto.get("classification"),
            "gauge_lower": dto.get("gaugeLowerLimit"),
            "gauge_upper": dto.get("gaugeUpperLimit"),
            "contributors": contributors,
        }), "No endurance score for this date.")

    return _run(fn)


def get_lactate_threshold() -> dict[str, Any]:
    """Latest running lactate threshold: heart rate, pace (min/km) and power if available.

    Use for setting threshold/tempo HR and pace zones."""

    def fn(g: Any) -> Any:
        lt = g.get_lactate_threshold() or {}
        shr = lt.get("speed_and_heart_rate") or {}
        pw = lt.get("power") or {}
        speed = shr.get("speed")
        # Garmin stores LT speed in tens of m/s (e.g. 0.31 == 3.1 m/s).
        mps = float(speed) * 10 if speed is not None and float(speed) < 1.5 else speed
        res = _clean({
            "date": shr.get("calendarDate"),
            "heart_rate": shr.get("heartRate"),
            "pace_min_km": pace(mps),
            "power_w": pw.get("functionalThresholdPower"),
            "power_w_per_kg": _r(pw.get("powerToWeight"), 2),
        })
        return res or {"message": "No lactate threshold recorded (needs a guided LT test or auto-detection with a chest strap)."}

    return _run(fn)


def _bb_levels(day: dict[str, Any]) -> list[tuple[float, float]]:
    """(timestamp, level) pairs from a body-battery day, using descriptors when present."""
    arr = day.get("bodyBatteryValuesArray") or []
    desc = day.get("bodyBatteryValueDescriptorDTOList") or day.get("bodyBatteryValueDescriptorsDTOList") or []
    idx = None
    for dd in desc:
        if "level" in str(dd.get("bodyBatteryValueDescriptorKey", "")).lower():
            idx = dd.get("bodyBatteryValueDescriptorIndex")
    out = []
    for row in arr:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        val = row[idx] if idx is not None and idx < len(row) else row[-1] if isinstance(row[-1], (int, float)) else row[1]
        if isinstance(val, (int, float)) and isinstance(row[0], (int, float)):
            out.append((row[0], val))
    return out


def get_body_battery(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Body Battery per day: charged, drained, highest, lowest, start-of-day and end-of-day level.

    Use for energy/recovery trends across several days.

    Args:
        start_date: YYYY-MM-DD, default 6 days before end_date.
        end_date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    end = _date(end_date)
    start = _date(start_date) if start_date else (dt.date.fromisoformat(end) - dt.timedelta(days=6)).isoformat()
    if start > end:
        raise ToolError("start_date is after end_date")
    if (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 31:
        raise ToolError("Range too large (max 31 days).")

    def fn(g: Any) -> Any:
        days = g.get_body_battery(start, end) or []
        out = []
        for day in days:
            lv = _bb_levels(day)
            vals = [v for _, v in lv]
            out.append(_clean({
                "date": day.get("date") or day.get("calendarDate"),
                "charged": day.get("charged"),
                "drained": day.get("drained"),
                "high": max(vals) if vals else None,
                "low": min(vals) if vals else None,
                "start_level": vals[0] if vals else None,
                "end_level": vals[-1] if vals else None,
            }))
        return {"start_date": start, "end_date": end, "days": out}

    return _run(fn)


def get_heart_rates(date: str | None = None) -> dict[str, Any]:
    """Heart rate across a day, summarized: resting/min/max, 7-day resting average and an
    hourly table (avg/min/max bpm) instead of every sample.

    Args:
        date: YYYY-MM-DD, default today (Asia/Jakarta).
    """
    d = _date(date)

    def fn(g: Any) -> Any:
        h = g.get_heart_rates(d) or {}
        hourly: dict[int, list[float]] = {}
        for row in h.get("heartRateValues") or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2 or row[1] is None:
                continue
            # timestamps are GMT epoch-ms; convert to Asia/Jakarta hour
            hour = dt.datetime.fromtimestamp(row[0] / 1000, tz=TZ).hour
            hourly.setdefault(hour, []).append(float(row[1]))
        table = [
            {"hour": f"{hr:02d}:00", "avg": _r(statistics.fmean(v), 0), "min": _r(min(v), 0), "max": _r(max(v), 0)}
            for hr, v in sorted(hourly.items())
        ]
        return _clean({
            "date": d,
            "resting_hr": h.get("restingHeartRate"),
            "min_hr": h.get("minHeartRate"),
            "max_hr": h.get("maxHeartRate"),
            "resting_hr_7d_avg": h.get("lastSevenDaysAvgRestingHeartRate"),
            "hourly": table,
        })

    return _run(fn)


TOOLS: list[Callable[..., Any]] = [
    list_activities, get_activity, get_daily_summary, get_sleep, get_hrv, get_training_readiness,
    get_training_status, get_max_metrics, get_race_predictions, get_personal_records,
    get_endurance_score, get_lactate_threshold, get_body_battery, get_heart_rates,
]


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def _transport_security() -> TransportSecuritySettings:
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not host:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=[f"https://{host}", "https://claude.ai", "https://claude.com",
                         "http://localhost:*", "http://127.0.0.1:*"],
    )


def build_mcp(secret: str) -> FastMCP:
    from mcp.types import ToolAnnotations

    mcp = FastMCP(
        "garmin",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path=f"/{secret}/mcp",
        transport_security=_transport_security(),
    )
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    for fn in TOOLS:
        mcp.add_tool(fn, annotations=ro)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


class RedactedAccessLog:
    """Minimal access log that never prints the secret path segment."""

    def __init__(self, app: Any, secret: str) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 0}

        async def _send(msg: dict[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = scope.get("path", "")
            shown = path.replace(self.secret, "<secret>") if self.secret else path
            if status["code"] == 404:
                shown = "<other>"
            log.info("%s %s -> %s", scope.get("method"), shown, status["code"])


def create_app(secret: str | None = None) -> Any:
    secret = secret or os.environ.get("MCP_PATH_SECRET", "")
    if len(secret) < 32:
        raise SystemExit("MCP_PATH_SECRET must be set to a random value of at least 32 characters "
                         "(python -c \"import secrets; print(secrets.token_urlsafe(32))\").")
    mcp = build_mcp(secret)
    return RedactedAccessLog(mcp.streamable_http_app(), secret)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "garminconnect", "curl_cffi", "mcp.server.streamable_http",
                  "mcp.server.lowlevel.server", "mcp.server.streamable_http_manager"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = create_app()
    port = int(os.environ.get("PORT", "8000"))
    log.info("Listening on 0.0.0.0:%s (MCP at /<secret>/mcp, health at /health)", port)
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
