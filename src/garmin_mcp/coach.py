"""Coaching logic: combines live Garmin data with the training plan and its rules."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any
from zoneinfo import ZoneInfo

from . import analysis as A
from .client import GarminClient
from .plan import (
    Plan,
    Step,
    flatten,
    pace_to_seconds,
    parse_session,
    seconds_to_hms,
    seconds_to_pace,
)


def today(plan: Plan) -> dt.date:
    return dt.datetime.now(ZoneInfo(plan.timezone)).date()


def parse_day(plan: Plan, value: str | None) -> dt.date:
    if not value or value.lower() == "today":
        return today(plan)
    if value.lower() == "yesterday":
        return today(plan) - dt.timedelta(days=1)
    if value.lower() == "tomorrow":
        return today(plan) + dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Fetch helpers
# --------------------------------------------------------------------------- #

def activities(client: GarminClient, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    res = client.call("get_activities_by_date", start.isoformat(), end.isoformat())
    return list(res or [])


def runs_between(client: GarminClient, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    return [a for a in activities(client, start, end) if A.is_run(a)]


def samples_for(client: GarminClient, activity_id: int | str) -> list[A.Sample]:
    details = client.safe("get_activity_details", str(activity_id))
    return A.samples_from_details(details) if details else []


# --------------------------------------------------------------------------- #
# Snapshot / wellness
# --------------------------------------------------------------------------- #

def athlete_snapshot(client: GarminClient, plan: Plan, day: dt.date) -> dict[str, Any]:
    d = day.isoformat()
    summary = client.safe("get_user_summary", d) or {}
    out = {
        "date": d,
        "sleep_last_night": A.sleep_summary(client.safe("get_sleep_data", d)),
        "hrv": A.hrv_summary(client.safe("get_hrv_data", d)),
        "training_readiness": A.readiness_summary(client.safe("get_training_readiness", d)),
        "resting_hr": summary.get("restingHeartRate"),
        "resting_hr_7d_avg": summary.get("lastSevenDaysAvgRestingHeartRate"),
        "body_battery": {
            "now": summary.get("bodyBatteryMostRecentValue"),
            "high": summary.get("bodyBatteryHighestValue"),
            "low": summary.get("bodyBatteryLowestValue"),
            "at_wake": summary.get("bodyBatteryAtWakeTime"),
        },
        "stress_avg": summary.get("averageStressLevel"),
        "steps": summary.get("totalSteps"),
        "training_status": A.training_status_summary(client.safe("get_training_status", d)),
        "race_predictions": A.race_predictions_summary(client.safe("get_race_predictions")),
    }
    return out


def wellness_trend(client: GarminClient, plan: Plan, end: dt.date, days: int = 14) -> dict[str, Any]:
    rows = []
    for i in range(days - 1, -1, -1):
        d = end - dt.timedelta(days=i)
        sl = A.sleep_summary(client.safe("get_sleep_data", d.isoformat()))
        us = client.safe("get_user_summary", d.isoformat()) or {}
        rows.append({
            "date": d.isoformat(),
            "sleep_h": sl.get("sleep_hours"),
            "sleep_score": sl.get("sleep_score"),
            "overnight_hrv": sl.get("avg_overnight_hrv"),
            "resting_hr": us.get("restingHeartRate") or sl.get("resting_hr"),
            "body_battery_high": us.get("bodyBatteryHighestValue"),
            "stress_avg": us.get("averageStressLevel"),
        })
    target = plan.rules.get("sleep_hours_target", 7)
    hours = [r["sleep_h"] for r in rows if r["sleep_h"]]
    rhr = [r["resting_hr"] for r in rows if r["resting_hr"]]
    return {
        "days": rows,
        "averages": {
            "sleep_h": round(statistics.fmean(hours), 2) if hours else None,
            "nights_meeting_sleep_target": sum(1 for h in hours if h >= target),
            "resting_hr": round(statistics.fmean(rhr), 1) if rhr else None,
        },
        "sleep_target_h": target,
    }


# --------------------------------------------------------------------------- #
# Fatigue / readiness (plan's disruption + regression rules)
# --------------------------------------------------------------------------- #

def fatigue_check(client: GarminClient, plan: Plan, day: dt.date) -> dict[str, Any]:
    cap = plan.rules.get("easy_hr_cap", 150)
    runs = runs_between(client, day - dt.timedelta(days=42), day)
    eff = A.easy_run_efficiency(runs, cap)
    drift = A.hr_drift_vs_baseline(eff)

    nights = []
    for i in range(6, -1, -1):
        d = day - dt.timedelta(days=i)
        s = A.sleep_summary(client.safe("get_sleep_data", d.isoformat()))
        nights.append({"date": d.isoformat(), "hours": s.get("sleep_hours"), "score": s.get("sleep_score"),
                       "resting_hr": s.get("resting_hr")})
    bad_streak = max_streak = 0
    for n in nights:
        bad = (n["hours"] is not None and n["hours"] < 6.5) or (n["score"] is not None and n["score"] < 60)
        bad_streak = bad_streak + 1 if bad else 0
        max_streak = max(max_streak, bad_streak)

    hrv = A.hrv_summary(client.safe("get_hrv_data", day.isoformat()))
    ready = A.readiness_summary(client.safe("get_training_readiness", day.isoformat()))
    status = A.training_status_summary(client.safe("get_training_status", day.isoformat()))

    flags: list[str] = []
    verdict = "proceed"
    hr_delta = drift.get("implied_hr_change_bpm_at_same_pace")
    if hr_delta is not None and hr_delta >= 5:
        flags.append(f"Easy-run HR is ~{hr_delta:g} bpm higher at the same pace over the last 3 easy runs (rule: ≥ 5 bpm for 3+ days).")
        verdict = "reduce_week_25"
    if max_streak >= 3:
        flags.append(f"{max_streak} bad nights of sleep in a row (< 6.5 h or score < 60).")
        verdict = "reduce_week_25"
    if verdict == "proceed":
        if ready.get("score") is not None and ready["score"] < 33:
            flags.append(f"Garmin training readiness is low ({ready['score']}).")
            verdict = "easy_today"
        hrv_status = str(hrv.get("status", "")).upper()
        if hrv_status in {"LOW", "POOR", "UNBALANCED"}:
            flags.append(f"HRV status is {hrv_status}.")
            verdict = "easy_today"
        acwr = status.get("acwr")
        if isinstance(acwr, (int, float)) and acwr > 1.5:
            flags.append(f"Acute:chronic load ratio is high ({acwr}).")
            verdict = "easy_today"
        if hr_delta is not None and 3 <= hr_delta < 5:
            flags.append(f"Easy HR drifting up ~{hr_delta:g} bpm – watch it, not yet at the 5 bpm rule.")

    actions = {
        "proceed": "Train as planned.",
        "easy_today": "Keep today's run easy (HR ≤ 150); if today is a key run, swap it to the other weekend day or run it as steady-only.",
        "reduce_week_25": "Plan rule: run this week at −25% volume (shorten every run, keep it easy), then continue from the previous level.",
    }
    return {
        "date": day.isoformat(),
        "verdict": verdict,
        "action": actions[verdict],
        "flags": flags,
        "easy_hr_drift": drift,
        "sleep_last_7_nights": nights,
        "hrv": hrv,
        "training_readiness": ready,
        "training_status": status,
        "plan_rules": plan.rules.get("disruption"),
    }


# --------------------------------------------------------------------------- #
# Plan views
# --------------------------------------------------------------------------- #

def today_plan(client: GarminClient | None, plan: Plan, day: dt.date) -> dict[str, Any]:
    dp = plan.day_plan(day)
    if dp is None:
        if day < plan.start_date:
            return {"date": day.isoformat(), "message": f"Plan starts {plan.start_date.isoformat()}."}
        return {"date": day.isoformat(), "message": "Plan finished (race was " + plan.race_date.isoformat() + ")."}

    out: dict[str, Any] = {"plan": dp}
    segs = parse_session(dp["session"])
    est_s = plan.estimate_seconds(segs, day)
    out["estimated_duration"] = seconds_to_hms(est_s) if segs else None
    zones_used = sorted({s.zone for s in flatten(segs)})
    out["targets"] = {z: plan.zones.get(z) for z in zones_used}

    notes = []
    if dp["role"] == "key_run":
        notes.append(f"Hard cap {plan.rules.get('key_run_max_minutes', 120)} min – stop at 2 h even if the distance isn't reached.")
        if est_s > 75 * 60:
            notes.append(plan.rules.get("fueling", ""))
    if dp.get("test"):
        notes.append("Test: same flat route, same start time as previous tests. Record the result.")
    if dp.get("strength"):
        notes.append(dp["strength"]["detail"])
    ms = plan.milestone_for(day)
    if ms:
        out["this_month_milestone"] = ms
    out["notes"] = [n for n in notes if n]

    if client is not None:
        done = [A.summarize_activity(a) for a in activities(client, day, day)]
        out["already_logged_today"] = done
        out["readiness"] = fatigue_check(client, plan, day)
        # trim the readiness payload for the daily view
        r = out["readiness"]
        out["readiness"] = {k: r[k] for k in ("verdict", "action", "flags", "training_readiness", "hrv")}
        out["readiness"]["sleep_last_night"] = r["sleep_last_7_nights"][-1]

    nxt = plan.day_plan(day + dt.timedelta(days=1))
    if nxt:
        out["tomorrow"] = {k: nxt.get(k) for k in ("date", "day", "role", "structure", "planned_km")}
    return out


# --------------------------------------------------------------------------- #
# Activity analysis
# --------------------------------------------------------------------------- #

def _zone_window(session: str, zone: str, plan: Plan, day: dt.date) -> tuple[float, float] | None:
    """Start/end seconds of the first step in `zone`, assuming steps run back-to-back."""
    t = 0.0
    for st in flatten(parse_session(session)):
        dur = st.seconds if st.seconds is not None else (st.km or 0) * plan.zone_pace_s(st.zone, day)
        if st.zone == zone:
            return t, t + dur
        t += dur
    return None


def analyze_activity(client: GarminClient, plan: Plan, activity_id: str) -> dict[str, Any]:
    act = client.call("get_activity", str(activity_id))
    summary = A.summarize_activity(act)
    day = A.activity_date(act)
    samples = samples_for(client, activity_id)
    out: dict[str, Any] = {"summary": summary}
    out["laps"] = A.laps_summary(client.safe("get_activity_splits", str(activity_id)))
    if samples:
        out["hr_distribution_plan_zones"] = A.hr_time_distribution(samples, plan.zones)
        out["aerobic_decoupling"] = A.aerobic_decoupling(samples)
        strides = A.count_strides(samples)
        if strides:
            out["strides_detected"] = strides
        tempo_lo = plan.zones.get("tempo", {}).get("min", 158)
        block = A.detect_hr_block(samples, tempo_lo)
        if block:
            out["tempo_block"] = block
    else:
        out["note"] = "No second-by-second data available; zone analysis skipped."

    if day:
        dp = plan.day_plan(day)
        if dp:
            out["planned"] = {k: dp.get(k) for k in ("date", "week", "phase", "role", "key_type", "structure", "planned_km", "test")}
            actual_km = summary.get("distance_km") or 0
            if dp["planned_km"]:
                out["compliance"] = {
                    "planned_km": dp["planned_km"],
                    "actual_km": actual_km,
                    "ratio": round(actual_km / dp["planned_km"], 2),
                }
            out["assessment"] = _assess(plan, dp, summary, out, samples, day)
            if dp.get("test"):
                out["test_result"] = analyze_test_samples(plan, dp["test"], samples, summary, day)
    return out


def _assess(plan: Plan, dp: dict[str, Any], summary: dict[str, Any], res: dict[str, Any],
            samples: list[A.Sample], day: dt.date) -> list[str]:
    notes: list[str] = []
    dist = res.get("hr_distribution_plan_zones") or {}
    share = dist.get("share_at_or_below_easy_cap")
    cap = plan.rules.get("easy_hr_cap", 150)
    role, kt = dp.get("role"), dp.get("key_type")
    if role in ("weekday_easy", "weekend_easy") or kt == "easy":
        if share is not None:
            ok = share >= 0.9
            notes.append(f"{share:.0%} of time at HR ≤ {cap} – {'good easy run' if ok else 'drifted above easy; slow down / walk hills'}.")
    if kt == "steady_lr":
        dec = (res.get("aerobic_decoupling") or {}).get("decoupling_pct")
        if dec is not None:
            notes.append(f"Decoupling {dec}% – {'comfortable at this distance' if dec < plan.rules.get('decoupling_ok_pct', 5) else 'not yet comfortable; repeat this distance before progressing'}.")
        lo, hi = plan.hr_range("steady")
        steady_win = _zone_window(dp["session"], "steady", plan, day)
        if steady_win and samples:
            wm = A.window_metrics(samples, steady_win[0], samples[-1].t, (lo, hi))
            if "share_in_target_hr" in wm:
                notes.append(f"Steady part: avg HR {wm.get('avg_hr')}, {wm['share_in_target_hr']:.0%} inside {lo}–{hi}, pace {wm.get('avg_pace')}.")
    if kt == "tempo_lr":
        blk = res.get("tempo_block")
        if blk:
            notes.append(f"Tempo block: {blk.get('distance_km')} km at {blk.get('avg_pace')} /km, avg HR {blk.get('avg_hr')} (milestone metric = this pace).")
        else:
            notes.append("No sustained tempo block (HR ≥ 158 for 5+ min) found.")
    if role == "key_run" and summary.get("moving_time"):
        secs = pace_to_seconds(summary["moving_time"]) or 0
        if secs > plan.rules.get("key_run_max_minutes", 120) * 60 + 300:
            notes.append("Ran > 2 h – plan caps key runs at 2 h to protect recovery.")
    if "strides" in dp.get("session", ""):
        n = res.get("strides_detected", 0)
        notes.append(f"Strides detected: {n}" + ("" if n >= 4 else " (plan: 6×20 s on Tuesdays)"))
    return notes


def analyze_test_samples(plan: Plan, test: dict[str, Any], samples: list[A.Sample],
                         summary: dict[str, Any], day: dt.date) -> dict[str, Any]:
    kind = test.get("kind")
    out: dict[str, Any] = {"test_id": test.get("id"), "kind": kind}
    if not samples:
        out["error"] = "No time-series data for this activity."
        return out
    if kind in ("aerobic", "tempo"):
        zone = "aerobic_test" if kind == "aerobic" else "tempo_test"
        win = _zone_window(test["session"], zone, plan, day)
        if not win:
            out["error"] = "Test session has no test-zone step."
            return out
        lo, hi = plan.hr_range(zone)
        m = A.window_metrics(samples, win[0], win[1], (lo, hi))
        out["core"] = m
        tgt = pace_to_seconds(test.get("target_pace"))
        if tgt and m.get("pace_s_per_km"):
            diff = m["pace_s_per_km"] - tgt
            out["target_pace"] = test.get("target_pace")
            out["vs_target_s_per_km"] = round(diff, 1)
            out["verdict"] = "on/ahead of target" if diff <= 0 else ("close (within 5 s/km)" if diff <= 5 else "behind target")
        if m.get("share_in_target_hr") is not None and m["share_in_target_hr"] < 0.7:
            out["warning"] = f"Only {m['share_in_target_hr']:.0%} of the core was inside {lo}–{hi} bpm – result is less comparable."
        return out
    if kind in ("time_trial", "race"):
        dist_km = float(test.get("distance_km") or 0)
        be = A.best_effort(samples, dist_km * 1000)
        if not be:
            out["error"] = f"Activity does not cover {dist_km} km."
            return out
        out["result"] = be
        t = be["time_s"]
        out["levels"] = {lvl: test.get(lvl) for lvl in ("stretch", "target", "pass") if test.get(lvl)}
        levels = [(lvl, pace_to_seconds(test.get(lvl))) for lvl in ("stretch", "target", "pass")
                  if test.get(lvl) and test.get(lvl) != "baseline"]
        achieved = next((lvl for lvl, v in levels if v and t <= v), None)
        out["achieved"] = achieved or ("baseline recorded" if test.get("target") == "baseline" else "below pass level")
        if dist_km and abs(dist_km - 21.0975) > 0.5:
            out["half_marathon_equivalent_riegel"] = seconds_to_hms(A.riegel(t, dist_km, 21.0975))
        if abs(dist_km - 10) < 0.01:
            rp = t / 10 + plan.rules.get("race_pace_from_10k_tt_offset_s", 15)
            out["race_pace_from_this_tt"] = seconds_to_pace(rp)
            out["race_finish_estimate"] = seconds_to_hms(rp * 21.0975)
        return out
    out["error"] = f"Unknown test kind {kind!r}"
    return out


def find_test_activity(client: GarminClient, test: dict[str, Any]) -> dict[str, Any] | None:
    d = test["date"] if isinstance(test["date"], dt.date) else dt.date.fromisoformat(test["date"])
    runs = runs_between(client, d, d)
    if not runs:
        return None
    return max(runs, key=lambda a: a.get("distance") or 0)


def analyze_test(client: GarminClient, plan: Plan, test_id: str | None, activity_id: str | None) -> dict[str, Any]:
    test = plan.test_by_id(test_id) if test_id else None
    act = None
    if activity_id:
        act = client.call("get_activity", str(activity_id))
        if test is None:
            d = A.activity_date(act)
            test = plan.test_on(d) if d else None
    if test is None:
        return {"error": "Unknown test – pass test_id (e.g. 'TT0', 'AER-2026-11') or an activity on a test date."}
    if act is None:
        found = find_test_activity(client, test)
        if not found:
            return {"test": _jsonable(test), "error": f"No run found on {test['date']}. Pass activity_id if it was done on another day."}
        act = found
    aid = act.get("activityId")
    day = A.activity_date(act) or test["date"]
    res = analyze_test_samples(plan, test, samples_for(client, aid), A.summarize_activity(act), day)
    res["activity"] = A.summarize_activity(act)
    res["test"] = _jsonable(test)
    return res


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in d.items()}


# --------------------------------------------------------------------------- #
# Weekly review
# --------------------------------------------------------------------------- #

def review_week(client: GarminClient, plan: Plan, day: dt.date) -> dict[str, Any]:
    idx = plan.week_index(day)
    if idx is None:
        return {"error": f"{day} is outside the plan ({plan.start_date} – {plan.race_date})."}
    week = plan.resolve_week(idx)
    start = dt.date.fromisoformat(week["start"])
    end = min(dt.date.fromisoformat(week["end"]), today(plan))
    acts = activities(client, start, end)
    runs = [a for a in acts if A.is_run(a)]
    strength = [a for a in acts if A.is_strength(a)]
    cap = plan.rules.get("easy_hr_cap", 150)

    by_date: dict[str, list[dict[str, Any]]] = {}
    for a in runs:
        by_date.setdefault(str(A.activity_date(a)), []).append(a)

    total_hr_s = under_cap_s = 0.0
    days_out = []
    strides_ok = None
    key_ok = None
    key_notes: list[str] = []
    for dp in week["days"]:
        d = dt.date.fromisoformat(dp["date"])
        day_runs = by_date.get(dp["date"], [])
        actual_km = round(sum((a.get("distance") or 0) for a in day_runs) / 1000, 2)
        row = {
            "date": dp["date"], "day": dp["day"], "role": dp["role"], "planned": dp["structure"],
            "planned_km": dp["planned_km"], "actual_km": actual_km,
            "runs": [A.summarize_activity(a) for a in day_runs],
        }
        if d > end:
            row["status"] = "upcoming"
        elif dp["planned_km"] and not day_runs:
            row["status"] = "missed"
        elif not dp["planned_km"] and day_runs:
            row["status"] = "extra"
        elif day_runs:
            row["status"] = "done"
        else:
            row["status"] = "rest"
        for a in day_runs:
            smp = samples_for(client, a["activityId"])
            if not smp:
                continue
            hd = A.hr_time_distribution(smp, plan.zones)
            total_hr_s += (hd["total_min_with_hr"] or 0) * 60
            under_cap_s += (hd["minutes_at_or_below_easy_cap"] or 0) * 60
            if dp["day"] == "Tuesday" and "strides" in dp["session"]:
                n = A.count_strides(smp)
                row["strides_detected"] = n
                strides_ok = n >= 4
            if dp["role"] == "key_run":
                dec = A.aerobic_decoupling(smp)
                row["decoupling_pct"] = dec.get("decoupling_pct")
        if dp["role"] == "key_run":
            row["_runs_raw"] = day_runs
        days_out.append(row)

    # Key run: on its planned day, or shifted to the other weekend day (weekend shift rule).
    key_row = next((r for r in days_out if r["role"] == "key_run"), None)
    if key_row is not None:
        key_runs = key_row.pop("_runs_raw")
        planned = key_row["planned_km"] or 0
        if key_runs:
            ratio = key_row["actual_km"] / planned if planned else 1
            dur = sum((a.get("movingDuration") or a.get("duration") or 0) for a in key_runs)
            key_ok = ratio >= 0.9 or dur >= 115 * 60
            key_notes.append(f"{key_row['actual_km']} of {planned} km planned"
                             + (" (stopped at the 2 h cap – counts)" if ratio < 0.9 and dur >= 115 * 60 else ""))
        elif dt.date.fromisoformat(key_row["date"]) <= end:
            other = [r for r in days_out if r["role"] == "weekend_easy" and r["actual_km"] >= 0.9 * planned]
            key_ok = bool(other) if other or end >= dt.date.fromisoformat(week["end"]) else None
            key_notes.append("Not run on the planned day" + (" – done on the other weekend day" if other else ""))

    weekday_planned = [r for r in days_out if r["role"] in ("weekday_easy", "test") and dt.date.fromisoformat(r["date"]).weekday() < 5]
    weekday_done = [r for r in weekday_planned if r["actual_km"] > 0]
    weekday_hot = [
        r for r in weekday_done
        if any((x.get("avg_hr") or 0) > cap for x in r["runs"]) and r["role"] != "test"
    ]
    strength_planned = [r for r in week["days"] if r.get("strength")]
    nights = []
    for i in range(7):
        d = start + dt.timedelta(days=i)
        if d > end:
            break
        nights.append(A.sleep_summary(client.safe("get_sleep_data", d.isoformat())).get("sleep_hours"))
    sleep_target = plan.rules.get("sleep_hours_target", 7)
    good_nights = sum(1 for h in nights if h and h >= sleep_target)

    share = under_cap_s / total_hr_s if total_hr_s else None
    checklist = {
        "weekday_runs": {
            "ok": len(weekday_done) >= len(weekday_planned) and not weekday_hot,
            "detail": f"{len(weekday_done)}/{len(weekday_planned)} planned weekday runs"
            + (f"; {len(weekday_hot)} ran above HR {cap} on average" if weekday_hot else ", all easy"),
        },
        "key_run": {"ok": key_ok, "detail": "; ".join(key_notes) or "not yet"},
        "legs": {
            "ok": len(strength) >= len(strength_planned) if strength_planned else True,
            "detail": f"{len(strength)} strength session(s) logged on Garmin / {len(strength_planned)} planned"
            + ("" if strength else " (log Leg A/B as Strength on your watch so they count)"),
        },
        "easy_share": {
            "ok": share is not None and share >= plan.rules.get("easy_share_target", 0.8),
            "detail": f"{share:.0%} of run time at HR ≤ {cap}" if share is not None else "no HR data",
        },
        "strides": {"ok": strides_ok, "detail": "detected from Tuesday speed bursts" if strides_ok is not None else "no Tuesday run with data"},
        "sleep": {
            "ok": good_nights >= plan.rules.get("sleep_nights_target", 5),
            "detail": f"{good_nights}/{len(nights)} nights ≥ {sleep_target} h so far",
        },
    }
    score = sum(1 for v in checklist.values() if v["ok"])
    pass_score = plan.raw.get("weekly_checklist", {}).get("pass_score", 5)
    actual_total = round(sum(r["actual_km"] for r in days_out), 1)
    nxt = plan.resolve_week(idx + 1) if idx + 1 < len(plan.weeks) else None
    return {
        "week": week["week"],
        "range": f"{week['start']} – {week['end']}",
        "phase": week["phase"],
        "complete": end >= dt.date.fromisoformat(week["end"]),
        "volume": {"planned_km": week["planned_total_km"], "actual_km": actual_total},
        "days": days_out,
        "checklist": checklist,
        "checklist_score": f"{score}/6",
        "week_successful": score >= pass_score,
        "rule": plan.raw.get("weekly_checklist", {}).get("hold_rule"),
        "next_week": {
            "week": nxt["week"], "phase": nxt["phase"], "planned_total_km": nxt["planned_total_km"],
            "key_run": next((d["structure"] for d in nxt["days"] if d["role"] == "key_run"), None),
        } if nxt else None,
        "notes": [
            "The checklist asks for ≥ 3 weekday runs, but Base 1 (Nov–early Feb) only schedules 2 – this review counts against what the week actually schedules.",
            "'Without walking' can't be read from Garmin data – confirm it yourself.",
        ],
    }


# --------------------------------------------------------------------------- #
# Progress & race outlook
# --------------------------------------------------------------------------- #

def weekly_volume(client: GarminClient, plan: Plan, end: dt.date, weeks: int = 8) -> list[dict[str, Any]]:
    end_idx = plan.week_index(end)
    if end_idx is None:
        end_idx = len(plan.weeks) - 1 if end > plan.race_date else 0
    first = max(0, end_idx - weeks + 1)
    start = plan.week_start(first)
    runs = runs_between(client, start, min(end, plan.week_start(end_idx) + dt.timedelta(days=6)))
    rows = []
    for i in range(first, end_idx + 1):
        ws = plan.week_start(i)
        we = ws + dt.timedelta(days=6)
        wk_runs = [a for a in runs if (d := A.activity_date(a)) and ws <= d <= we]
        km = sum((a.get("distance") or 0) for a in wk_runs) / 1000
        long_run = max(((a.get("distance") or 0) / 1000 for a in wk_runs), default=0)
        rows.append({
            "week": i, "start": ws.isoformat(), "phase": plan.weeks[i].get("phase"),
            "planned_km": plan.weeks[i].get("total_km"), "actual_km": round(km, 1),
            "runs": len(wk_runs), "longest_km": round(long_run, 1),
        })
    return rows


def progress(client: GarminClient, plan: Plan, day: dt.date) -> dict[str, Any]:
    results = []
    prev_aer: float | None = None
    for t in plan.tests():
        if t["date"] > day:
            results.append({"test": t["id"], "date": t["date"].isoformat(), "kind": t["kind"], "status": "upcoming",
                            "target": t.get("target") or t.get("target_pace")})
            continue
        act = find_test_activity(client, t)
        if not act:
            results.append({"test": t["id"], "date": t["date"].isoformat(), "kind": t["kind"], "status": "no run logged that day"})
            continue
        r = analyze_test_samples(plan, t, samples_for(client, act["activityId"]), A.summarize_activity(act), t["date"])
        row: dict[str, Any] = {"test": t["id"], "date": t["date"].isoformat(), "kind": t["kind"], "status": "done",
                               "activity_id": act["activityId"]}
        if t["kind"] == "aerobic":
            pace_s = (r.get("core") or {}).get("pace_s_per_km")
            row.update(pace=seconds_to_pace(pace_s), avg_hr=(r.get("core") or {}).get("avg_hr"),
                       target=t.get("target_pace"), vs_target_s=r.get("vs_target_s_per_km"))
            row["trend_vs_previous"] = A.classify_monthly_change(prev_aer, pace_s)
            prev_aer = pace_s or prev_aer
        elif t["kind"] == "tempo":
            row.update(pace=(r.get("core") or {}).get("avg_pace"), avg_hr=(r.get("core") or {}).get("avg_hr"),
                       target=t.get("target_pace"), vs_target_s=r.get("vs_target_s_per_km"))
        else:
            row.update(time=(r.get("result") or {}).get("time"), achieved=r.get("achieved"),
                       hm_equivalent=r.get("half_marathon_equivalent_riegel"),
                       race_pace=r.get("race_pace_from_this_tt"))
        if r.get("error"):
            row["error"] = r["error"]
        results.append(row)
    return {
        "as_of": day.isoformat(),
        "tests": results,
        "weekly_volume_last_8": weekly_volume(client, plan, day, 8),
        "plateau_rules": plan.rules.get("plateau_rules"),
    }


def race_outlook(client: GarminClient, plan: Plan, day: dt.date) -> dict[str, Any]:
    prog = progress(client, plan, day)
    tts = [t for t in prog["tests"] if t["kind"] == "time_trial" and t.get("time")]
    latest = tts[-1] if tts else None
    goal = plan.raw["goal"]
    out: dict[str, Any] = {
        "goal": {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in goal.items()},
        "days_to_race": (plan.race_date - day).days,
        "garmin_race_predictions": A.race_predictions_summary(client.safe("get_race_predictions")),
        "latest_time_trial": latest,
        "race_pace_table_from_10k_tt": [
            {"tt_10k": f"{m}:00", "race_pace": seconds_to_pace(m * 6 + plan.rules.get("race_pace_from_10k_tt_offset_s", 15)),
             "finish": seconds_to_hms((m * 6 + plan.rules.get("race_pace_from_10k_tt_offset_s", 15)) * 21.0975)}
            for m in range(52, 58)
        ],
        "race_execution": plan.rules.get("race_execution"),
        "backup_path": plan.rules.get("backup_path"),
    }
    if latest and latest.get("hm_equivalent"):
        hm = pace_to_seconds(latest["hm_equivalent"]) or 0
        tgt = pace_to_seconds(goal["target"]) or 0
        out["gap_to_target"] = seconds_to_hms(hm - tgt) if hm > tgt else "already at/under target pace equivalent"
    if plan.race_pace_s_per_km:
        out["race_pace_in_use"] = seconds_to_pace(plan.race_pace_s_per_km)
    return out


def step_list(session: str) -> list[Step]:
    return flatten(parse_session(session))
