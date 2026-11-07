"""Pure analysis functions over Garmin Connect JSON. No network access here."""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from .plan import seconds_to_hms, seconds_to_pace


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safely walk nested dicts/lists."""
    cur = obj
    for p in path:
        if cur is None:
            return default
        if isinstance(p, int):
            if not isinstance(cur, list) or len(cur) <= p:
                return default
            cur = cur[p]
        else:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(p)
    return default if cur is None else cur


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def speed_to_pace(mps: float | None) -> str | None:
    if not mps or mps <= 0:
        return None
    return seconds_to_pace(1000.0 / mps)


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #

def activity_type(a: dict[str, Any]) -> str:
    return str(dig(a, "activityType", "typeKey", default="") or dig(a, "activityTypeDTO", "typeKey", default=""))


def is_run(a: dict[str, Any]) -> bool:
    return "running" in activity_type(a)


def is_strength(a: dict[str, Any]) -> bool:
    t = activity_type(a)
    return "strength" in t or t in {"indoor_cardio", "hiit"}


def activity_date(a: dict[str, Any]) -> dt.date | None:
    s = a.get("startTimeLocal") or dig(a, "summaryDTO", "startTimeLocal")
    if not s:
        return None
    return dt.date.fromisoformat(str(s)[:10])


def summarize_activity(a: dict[str, Any]) -> dict[str, Any]:
    """Compact summary of an activity from the list endpoint (or a detail summaryDTO)."""
    s = a.get("summaryDTO", a)
    dist_m = s.get("distance") or 0.0
    dur = s.get("movingDuration") or s.get("duration") or 0.0
    speed = s.get("averageSpeed") or (dist_m / dur if dur else None)
    zones = {
        f"z{i}": _r((a.get(f"hrTimeInZone_{i}") or 0) / 60, 1)
        for i in range(1, 6)
        if a.get(f"hrTimeInZone_{i}") is not None
    }
    out = {
        "activity_id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": activity_type(a),
        "start_local": a.get("startTimeLocal") or s.get("startTimeLocal"),
        "distance_km": _r(dist_m / 1000, 2),
        "moving_time": seconds_to_hms(dur),
        "elapsed_time": seconds_to_hms(s.get("elapsedDuration") or s.get("duration")),
        "avg_pace": speed_to_pace(speed) if is_run(a) or "averageSpeed" in s else None,
        "avg_hr": _r(s.get("averageHR"), 0),
        "max_hr": _r(s.get("maxHR"), 0),
        "elevation_gain_m": _r(s.get("elevationGain"), 0),
        "cadence_spm": _r(s.get("averageRunningCadenceInStepsPerMinute") or s.get("averageRunCadence"), 0),
        "aerobic_te": _r(s.get("aerobicTrainingEffect") or a.get("aerobicTrainingEffect")),
        "anaerobic_te": _r(s.get("anaerobicTrainingEffect") or a.get("anaerobicTrainingEffect")),
        "training_load": _r(s.get("activityTrainingLoad") or a.get("activityTrainingLoad"), 0),
        "te_label": a.get("trainingEffectLabel") or s.get("trainingEffectLabel"),
        "garmin_hr_zone_minutes": zones or None,
    }
    return {k: v for k, v in out.items() if v not in (None, {}, "")}


# --------------------------------------------------------------------------- #
# Time series
# --------------------------------------------------------------------------- #

@dataclass
class Sample:
    t: float  # seconds from start (elapsed)
    dist: float | None  # metres from start
    hr: float | None
    speed: float | None  # m/s


def samples_from_details(details: dict[str, Any]) -> list[Sample]:
    """Turn get_activity_details() output into a list of samples."""
    desc = details.get("metricDescriptors") or []
    idx = {d.get("key"): d.get("metricsIndex") for d in desc}
    rows = details.get("activityDetailMetrics") or []

    def get(row: list[Any], key: str) -> float | None:
        i = idx.get(key)
        if i is None or i >= len(row):
            return None
        v = row[i]
        return float(v) if v is not None else None

    out: list[Sample] = []
    t0 = None
    for r in rows:
        m = r.get("metrics") or []
        t = get(m, "sumDuration")
        if t is None:
            ts = get(m, "directTimestamp")
            if ts is None:
                continue
            t0 = ts if t0 is None else t0
            t = (ts - t0) / 1000.0
        out.append(
            Sample(
                t=t,
                dist=get(m, "sumDistance"),
                hr=get(m, "directHeartRate"),
                speed=get(m, "directSpeed") if "directSpeed" in idx else get(m, "directEnhancedSpeed"),
            )
        )
    out.sort(key=lambda s: s.t)
    return out


def _intervals(samples: list[Sample]) -> Iterable[tuple[Sample, float]]:
    """Yield (sample, duration until next sample), capping gaps at 30 s (pauses)."""
    for a, b in zip(samples, samples[1:]):
        d = b.t - a.t
        if 0 < d <= 30:
            yield a, d


def hr_time_distribution(samples: list[Sample], zones: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Minutes and share of time per plan HR zone, plus time at/below the easy cap."""
    bands = [
        ("recovery", None, zones.get("recovery", {}).get("max", 137)),
        ("easy", zones.get("easy", {}).get("min", 138), zones.get("easy", {}).get("max", 150)),
        ("steady", (zones.get("easy", {}).get("max", 150)) + 1, zones.get("steady", {}).get("max", 155)),
        ("gray", zones.get("steady", {}).get("max", 155) + 1, zones.get("tempo", {}).get("min", 158) - 1),
        ("tempo", zones.get("tempo", {}).get("min", 158), zones.get("tempo", {}).get("max", 165)),
        ("above_tempo", zones.get("tempo", {}).get("max", 165) + 1, None),
    ]
    cap = zones.get("easy", {}).get("max", 150)
    secs = {b[0]: 0.0 for b in bands}
    total = under_cap = 0.0
    for s, d in _intervals(samples):
        if s.hr is None or s.hr <= 0:
            continue
        total += d
        if s.hr <= cap:
            under_cap += d
        for name, lo, hi in bands:
            if (lo is None or s.hr >= lo) and (hi is None or s.hr <= hi):
                secs[name] += d
                break
    return {
        "total_min_with_hr": _r(total / 60),
        "minutes_at_or_below_easy_cap": _r(under_cap / 60),
        "share_at_or_below_easy_cap": _r(under_cap / total, 3) if total else None,
        "minutes_by_zone": {k: _r(v / 60) for k, v in secs.items()},
    }


def window_metrics(samples: list[Sample], start_s: float, end_s: float,
                   hr_range: tuple[float | None, float | None] | None = None) -> dict[str, Any]:
    win = [s for s in samples if start_s <= s.t <= end_s]
    if len(win) < 2:
        return {"error": "not enough samples in window"}
    dur = win[-1].t - win[0].t
    dist = None
    if win[0].dist is not None and win[-1].dist is not None:
        dist = win[-1].dist - win[0].dist
    hrs = [s.hr for s in win if s.hr]
    out: dict[str, Any] = {
        "window": f"{seconds_to_hms(start_s)}–{seconds_to_hms(win[-1].t)}",
        "duration": seconds_to_hms(dur),
        "distance_km": _r(dist / 1000, 2) if dist else None,
        "avg_pace": seconds_to_pace(dur / (dist / 1000)) if dist else None,
        "pace_s_per_km": _r(dur / (dist / 1000), 1) if dist else None,
        "avg_hr": _r(statistics.fmean(hrs), 1) if hrs else None,
    }
    if hr_range and hrs:
        lo, hi = hr_range
        inside = sum(1 for h in hrs if (lo is None or h >= lo) and (hi is None or h <= hi))
        out["share_in_target_hr"] = _r(inside / len(hrs), 3)
    return out


def aerobic_decoupling(samples: list[Sample], skip_first_s: float = 600, until_s: float | None = None) -> dict[str, Any]:
    """Pa:HR decoupling: change in efficiency factor (speed/HR) from first to second half."""
    win = [s for s in samples if s.t >= skip_first_s and (until_s is None or s.t <= until_s)
           and s.hr and s.hr > 0 and s.speed is not None and s.speed > 0.5]
    if len(win) < 20:
        return {"error": "not enough HR+speed samples"}
    mid_t = win[0].t + (win[-1].t - win[0].t) / 2
    first = [s for s in win if s.t <= mid_t]
    second = [s for s in win if s.t > mid_t]

    def ef(part: list[Sample]) -> tuple[float, float, float]:
        sp = statistics.fmean(s.speed for s in part)  # type: ignore[misc]
        hr = statistics.fmean(s.hr for s in part)  # type: ignore[misc]
        return sp / hr, sp, hr

    ef1, sp1, hr1 = ef(first)
    ef2, sp2, hr2 = ef(second)
    pct = (ef1 - ef2) / ef1 * 100
    return {
        "decoupling_pct": _r(pct, 1),
        "first_half": {"avg_pace": speed_to_pace(sp1), "avg_hr": _r(hr1, 0)},
        "second_half": {"avg_pace": speed_to_pace(sp2), "avg_hr": _r(hr2, 0)},
        "interpretation": "comfortable at this distance (< 5%)" if pct < 5 else "not yet comfortable (≥ 5%) – aerobic durability still limiting",
    }


def _smooth_hr(samples: list[Sample], window_s: float = 30) -> list[float | None]:
    out: list[float | None] = []
    j = 0
    for i, s in enumerate(samples):
        while samples[j].t < s.t - window_s:
            j += 1
        vals = [x.hr for x in samples[j : i + 1] if x.hr]
        out.append(statistics.fmean(vals) if vals else None)
    return out


def detect_hr_block(samples: list[Sample], hr_min: float, min_duration_s: float = 300,
                    max_gap_s: float = 45) -> dict[str, Any] | None:
    """Find the longest continuous block where smoothed HR ≥ hr_min (e.g. a tempo block)."""
    if not samples:
        return None
    sm = _smooth_hr(samples)
    best: tuple[int, int] | None = None
    start = None
    last_in = None
    for i, (s, h) in enumerate(zip(samples, sm)):
        if h is not None and h >= hr_min:
            if start is None:
                start = i
            last_in = i
        elif start is not None and last_in is not None and s.t - samples[last_in].t > max_gap_s:
            if best is None or samples[last_in].t - samples[start].t > samples[best[1]].t - samples[best[0]].t:
                best = (start, last_in)
            start = None
    if start is not None and last_in is not None:
        if best is None or samples[last_in].t - samples[start].t > samples[best[1]].t - samples[best[0]].t:
            best = (start, last_in)
    if best is None:
        return None
    a, b = samples[best[0]], samples[best[1]]
    if b.t - a.t < min_duration_s:
        return None
    return window_metrics(samples, a.t, b.t)


def count_strides(samples: list[Sample], min_s: float = 8, max_s: float = 45) -> int:
    """Count short speed bursts (strides): speed ≥ 1.3× the run's median for 8–45 s."""
    speeds = [s.speed for s in samples if s.speed and s.speed > 0.5]
    if len(speeds) < 30:
        return 0
    thresh = statistics.median(speeds) * 1.3
    count = 0
    start = None
    for s in samples:
        fast = s.speed is not None and s.speed >= thresh
        if fast and start is None:
            start = s.t
        elif not fast and start is not None:
            if min_s <= s.t - start <= max_s:
                count += 1
            start = None
    return count


def best_effort(samples: list[Sample], distance_m: float) -> dict[str, Any] | None:
    """Fastest continuous segment covering `distance_m` (two-pointer over cumulative distance)."""
    pts = [s for s in samples if s.dist is not None]
    if len(pts) < 2 or (pts[-1].dist or 0) - (pts[0].dist or 0) < distance_m * 0.98:
        return None
    best = None
    j = 0
    for i in range(len(pts)):
        while j < len(pts) and (pts[j].dist or 0) - (pts[i].dist or 0) < distance_m:
            j += 1
        if j >= len(pts):
            break
        covered = (pts[j].dist or 0) - (pts[i].dist or 0)
        t = (pts[j].t - pts[i].t) * distance_m / covered
        if best is None or t < best[0]:
            best = (t, i, j)
    if best is None:
        # whole activity is ~the distance (within 2%)
        t = (pts[-1].t - pts[0].t) * distance_m / ((pts[-1].dist or 0) - (pts[0].dist or 1))
        best = (t, 0, len(pts) - 1)
    t, i, j = best
    seg = pts[i : j + 1]
    last10 = [s.hr for s in seg if s.hr and s.t >= seg[-1].t - 600]
    half = seg[0].t + (seg[-1].t - seg[0].t) / 2
    d_half = None
    for s in seg:
        if s.t >= half:
            d_half = (s.dist or 0) - (seg[0].dist or 0)
            break
    split = None
    if d_half:
        first_pace = (half - seg[0].t) / (d_half / 1000)
        second_pace = (seg[-1].t - half) / (((seg[-1].dist or 0) - (seg[0].dist or 0) - d_half) / 1000 or 1)
        split = {
            "first_half_pace": seconds_to_pace(first_pace),
            "second_half_pace": seconds_to_pace(second_pace),
            "negative_split": second_pace < first_pace,
        }
    return {
        "distance_km": _r(distance_m / 1000, 3),
        "time": seconds_to_hms(t),
        "time_s": _r(t, 0),
        "avg_pace": seconds_to_pace(t / (distance_m / 1000)),
        "avg_hr_last_10_min": _r(statistics.fmean(last10), 0) if last10 else None,
        "split": split,
    }


def riegel(time_s: float, d1_km: float, d2_km: float, exponent: float = 1.06) -> float:
    return time_s * (d2_km / d1_km) ** exponent


def laps_summary(splits: dict[str, Any] | None) -> list[dict[str, Any]]:
    laps = (splits or {}).get("lapDTOs") or []
    out = []
    for lap in laps:
        dist = lap.get("distance") or 0
        dur = lap.get("movingDuration") or lap.get("duration") or 0
        out.append({
            "lap": lap.get("lapIndex"),
            "distance_km": _r(dist / 1000, 2),
            "time": seconds_to_hms(dur),
            "pace": seconds_to_pace(dur / (dist / 1000)) if dist else None,
            "avg_hr": _r(lap.get("averageHR"), 0),
            "max_hr": _r(lap.get("maxHR"), 0),
        })
    return out


# --------------------------------------------------------------------------- #
# Wellness
# --------------------------------------------------------------------------- #

def sleep_summary(sleep: dict[str, Any] | None) -> dict[str, Any]:
    d = dig(sleep, "dailySleepDTO", default={}) or {}
    secs = d.get("sleepTimeSeconds")
    return {
        k: v
        for k, v in {
            "date": d.get("calendarDate"),
            "sleep_hours": _r(secs / 3600, 2) if secs else None,
            "deep_min": _r((d.get("deepSleepSeconds") or 0) / 60, 0) if d.get("deepSleepSeconds") else None,
            "rem_min": _r((d.get("remSleepSeconds") or 0) / 60, 0) if d.get("remSleepSeconds") else None,
            "awake_min": _r((d.get("awakeSleepSeconds") or 0) / 60, 0) if d.get("awakeSleepSeconds") else None,
            "sleep_score": dig(d, "sleepScores", "overall", "value"),
            "bed_time_local": _ts_local(d.get("sleepStartTimestampLocal")),
            "wake_time_local": _ts_local(d.get("sleepEndTimestampLocal")),
            "resting_hr": dig(sleep, "restingHeartRate"),
            "avg_overnight_hrv": dig(sleep, "avgOvernightHrv"),
            "body_battery_change": dig(sleep, "bodyBatteryChange"),
        }.items()
        if v is not None
    }


def _ts_local(ms: Any) -> str | None:
    if not ms:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ms) / 1000, tz=dt.timezone.utc).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return None


def hrv_summary(hrv: dict[str, Any] | None) -> dict[str, Any]:
    s = dig(hrv, "hrvSummary", default={}) or {}
    base = s.get("baseline") or {}
    out = {
        "last_night_avg": s.get("lastNightAvg"),
        "weekly_avg": s.get("weeklyAvg"),
        "status": s.get("status"),
        "baseline_low": base.get("balancedLow"),
        "baseline_high": base.get("balancedUpper"),
        "feedback": s.get("feedbackPhrase"),
    }
    return {k: v for k, v in out.items() if v is not None}


def readiness_summary(tr: Any) -> dict[str, Any]:
    item = tr[0] if isinstance(tr, list) and tr else tr if isinstance(tr, dict) else {}
    out = {
        "score": item.get("score"),
        "level": item.get("level"),
        "feedback": item.get("feedbackShort") or item.get("feedbackLong"),
        "recovery_time_h": _r((item.get("recoveryTime") or 0) / 60, 1) if item.get("recoveryTime") else None,
        "sleep_score_factor": item.get("sleepScoreFactorPercent"),
        "hrv_factor": item.get("hrvFactorPercent"),
        "acute_load": item.get("acuteLoad"),
    }
    return {k: v for k, v in out.items() if v is not None}


def training_status_summary(ts: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    vo2 = dig(ts, "mostRecentVO2Max", "generic", default={}) or {}
    if vo2:
        out["vo2max"] = vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")
        out["vo2max_date"] = vo2.get("calendarDate")
    latest = dig(ts, "mostRecentTrainingStatus", "latestTrainingStatusData", default={}) or {}
    for dev in latest.values():
        if not isinstance(dev, dict):
            continue
        out["status"] = dev.get("trainingStatusFeedbackPhrase") or dev.get("trainingStatus")
        acute = dev.get("acuteTrainingLoadDTO") or {}
        out["acute_load"] = acute.get("dailyTrainingLoadAcute")
        out["chronic_load"] = acute.get("dailyTrainingLoadChronic")
        out["acwr"] = acute.get("dailyAcuteChronicWorkloadRatio") or acute.get("acwrPercent")
        out["acwr_status"] = acute.get("acwrStatus")
        break
    bal = dig(ts, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap", default={}) or {}
    for dev in bal.values():
        if isinstance(dev, dict):
            out["load_balance"] = {
                "low_aerobic": dev.get("monthlyLoadAerobicLow"),
                "high_aerobic": dev.get("monthlyLoadAerobicHigh"),
                "anaerobic": dev.get("monthlyLoadAnaerobic"),
                "feedback": dev.get("trainingBalanceFeedbackPhrase"),
            }
            break
    return {k: v for k, v in out.items() if v is not None}


def race_predictions_summary(rp: Any) -> dict[str, Any]:
    if isinstance(rp, list):
        rp = rp[-1] if rp else {}
    rp = rp or {}
    out = {
        "5k": seconds_to_hms(rp.get("time5K")),
        "10k": seconds_to_hms(rp.get("time10K")),
        "half_marathon": seconds_to_hms(rp.get("timeHalfMarathon")),
        "marathon": seconds_to_hms(rp.get("timeMarathon")),
        "date": rp.get("calendarDate"),
    }
    return {k: v for k, v in out.items() if v}


# --------------------------------------------------------------------------- #
# Trends / fatigue
# --------------------------------------------------------------------------- #

def easy_run_efficiency(runs: list[dict[str, Any]], easy_cap: float) -> list[dict[str, Any]]:
    """Per easy run: speed/HR efficiency. A run counts as easy if avg HR ≤ cap and ≥ 20 min."""
    out = []
    for a in runs:
        hr = a.get("averageHR")
        sp = a.get("averageSpeed")
        dur = a.get("movingDuration") or a.get("duration") or 0
        if not hr or not sp or dur < 1200 or hr > easy_cap:
            continue
        out.append({
            "date": str(a.get("startTimeLocal", ""))[:10],
            "activity_id": a.get("activityId"),
            "avg_hr": hr,
            "speed": sp,
            "pace": speed_to_pace(sp),
            "ef": sp / hr,
        })
    out.sort(key=lambda r: r["date"])
    return out


def hr_drift_vs_baseline(eff: list[dict[str, Any]], recent_n: int = 3) -> dict[str, Any]:
    """Compare the last `recent_n` easy runs with the earlier ones: implied HR change at the same pace."""
    if len(eff) < recent_n + 3:
        return {"status": "insufficient_data", "easy_runs": len(eff)}
    base = eff[:-recent_n]
    recent = eff[-recent_n:]
    ef_b = statistics.fmean(r["ef"] for r in base)
    ef_r = statistics.fmean(r["ef"] for r in recent)
    v_b = statistics.fmean(r["speed"] for r in base)
    hr_delta = v_b * (1 / ef_r - 1 / ef_b)
    pace_delta = (1000 / (ef_r * statistics.fmean(r["avg_hr"] for r in base))) - (1000 / (ef_b * statistics.fmean(r["avg_hr"] for r in base)))
    return {
        "status": "ok",
        "baseline_runs": len(base),
        "recent_runs": [r["date"] for r in recent],
        "baseline_pace_at_avg_hr": speed_to_pace(v_b),
        "implied_hr_change_bpm_at_same_pace": _r(hr_delta, 1),
        "implied_pace_change_s_per_km_at_same_hr": _r(pace_delta, 1),
    }


def classify_monthly_change(prev_pace_s: float | None, cur_pace_s: float | None) -> str | None:
    """Plan rule labels for month-over-month easy-test pace change (positive = faster)."""
    if prev_pace_s is None or cur_pace_s is None:
        return None
    gain = prev_pace_s - cur_pace_s
    if gain >= 5:
        return "improving"
    if gain <= -5:
        return "regressing"
    if abs(gain) <= 3:
        return "flat"
    return "slowing"
