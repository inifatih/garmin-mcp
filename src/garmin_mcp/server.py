"""MCP server exposing live Garmin Connect data + the training plan as coaching tools."""

from __future__ import annotations

import datetime as dt
import functools
import logging
import os
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import analysis as A
from . import coach
from .client import GarminAPIError, GarminAuthError, GarminClient
from .plan import load_plan, parse_session
from .workouts import build_workout, workout_name_for

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Personal Garmin coach server for one athlete training for a sub-2:00 half marathon (JRF 2027).

How to coach with these tools:
- Start a daily check-in with `get_today` – it returns the planned session, readiness verdict and notes.
- For "how did my run go?" use `list_activities` then `analyze_activity`.
- Every Sunday evening run `review_week` (the plan's 6-item checklist; 5/6 = successful week).
- Test days (time trials, monthly aerobic/tempo tests) → `analyze_test`; long-term view → `get_progress`.
- When fatigue signs appear use `check_fatigue`; the plan's rules decide (≥ 5 bpm easy-HR drift or
  bad-sleep streak → one week at −25%).
- To put a session on the watch, call `push_workout` with confirm=false first to preview, then confirm=true.
Coaching principles from the plan: HR beats pace (humid mornings are 15–30 s/km slower at the same HR);
≥ 80% of running time at HR ≤ 150; key run capped at 2 h; never make up missed runs.
Answer in the athlete's language (Indonesian or English, whichever they use).
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

mcp = MCPServer(name="garmin-coach", instructions=INSTRUCTIONS, version="0.1.0")
_client = GarminClient()

T = TypeVar("T", bound=Callable[..., Any])


def _errors(fn: T) -> T:
    """Turn auth/API/validation errors into clean tool errors."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (GarminAuthError, GarminAPIError) as e:
            raise ToolError(str(e)) from e
        except ValueError as e:
            raise ToolError(f"Invalid input: {e}") from e

    return wrapper  # type: ignore[return-value]


def _plan():
    return load_plan()


def _day(value: str | None) -> dt.date:
    return coach.parse_day(_plan(), value)


# --------------------------------------------------------------------------- #
# Plan tools (no Garmin access needed)
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_errors
def get_plan_overview() -> dict[str, Any]:
    """Goal, HR zones, phases, monthly milestones, test calendar, weekly checklist and all plan rules."""
    return _plan().overview()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_errors
def get_planned_week(date: str | None = None, week: int | None = None) -> dict[str, Any]:
    """The planned week (Mon–Sun) with a concrete session for each day.

    Args:
        date: Any date in the week (YYYY-MM-DD, 'today', 'tomorrow'). Defaults to today.
        week: Plan week number (0–50) instead of a date.
    """
    plan = _plan()
    if week is None:
        idx = plan.week_index(_day(date))
        if idx is None:
            raise ToolError("Date is outside the plan.")
    else:
        if not 0 <= week < len(plan.weeks):
            raise ToolError(f"week must be 0–{len(plan.weeks) - 1}")
        idx = week
    return plan.resolve_week(idx)


# --------------------------------------------------------------------------- #
# Live data
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=READ_ONLY)
@_errors
def get_today(date: str | None = None) -> dict[str, Any]:
    """Daily coaching check-in: today's planned session (structure, HR targets, est. duration), what's
    already logged today, readiness verdict (sleep, HRV, Garmin readiness, easy-HR drift) and tomorrow.

    Args:
        date: YYYY-MM-DD, 'today', 'tomorrow' or 'yesterday'. Defaults to today (Asia/Jakarta).
    """
    plan = _plan()
    return coach.today_plan(_client, plan, _day(date))


@mcp.tool(annotations=READ_ONLY)
@_errors
def get_athlete_snapshot(date: str | None = None) -> dict[str, Any]:
    """Live physiology for a day: last night's sleep, HRV status, training readiness, resting HR,
    body battery, stress, Garmin training status (VO2max, acute/chronic load) and race predictions."""
    return coach.athlete_snapshot(_client, _plan(), _day(date))


@mcp.tool(annotations=READ_ONLY)
@_errors
def get_wellness_trend(days: int = 14, end_date: str | None = None) -> dict[str, Any]:
    """Day-by-day sleep hours/score, overnight HRV, resting HR, body battery and stress.

    Args:
        days: Number of days back (1–42).
        end_date: Last day (default today).
    """
    days = max(1, min(days, 42))
    return coach.wellness_trend(_client, _plan(), _day(end_date), days)


@mcp.tool(annotations=READ_ONLY)
@_errors
def list_activities(start_date: str | None = None, end_date: str | None = None,
                    runs_only: bool = False) -> list[dict[str, Any]]:
    """Activities between two dates (inclusive), newest last, with distance, pace, HR, training effect.

    Args:
        start_date: YYYY-MM-DD (default: 7 days ago).
        end_date: YYYY-MM-DD (default: today).
        runs_only: Only running activities.
    """
    end = _day(end_date)
    start = _day(start_date) if start_date else end - dt.timedelta(days=7)
    if (end - start).days > 366:
        raise ToolError("Range too large (max 366 days).")
    acts = coach.activities(_client, start, end)
    if runs_only:
        acts = [a for a in acts if A.is_run(a)]
    acts.sort(key=lambda a: a.get("startTimeLocal") or "")
    return [A.summarize_activity(a) for a in acts]


@mcp.tool(annotations=READ_ONLY)
@_errors
def analyze_activity(activity_id: str) -> dict[str, Any]:
    """Deep analysis of one activity against the plan: laps, time in plan HR zones, share at HR ≤ 150,
    aerobic decoupling (Pa:HR), tempo-block detection, strides count, planned-vs-actual and a coach
    assessment. Test days also include the test result."""
    return coach.analyze_activity(_client, _plan(), activity_id)


@mcp.tool(annotations=READ_ONLY)
@_errors
def get_training_status(date: str | None = None) -> dict[str, Any]:
    """Garmin's training status, VO2max, acute/chronic load & ACWR, load balance, race predictions,
    lactate threshold and endurance score."""
    d = _day(date)
    out = {
        "training_status": A.training_status_summary(_client.safe("get_training_status", d.isoformat())),
        "race_predictions": A.race_predictions_summary(_client.safe("get_race_predictions")),
    }
    lt = _client.safe("get_lactate_threshold")
    if lt:
        out["lactate_threshold"] = lt
    es = _client.safe("get_endurance_score", (d - dt.timedelta(days=28)).isoformat(), d.isoformat())
    if es:
        out["endurance_score"] = {k: es.get(k) for k in ("avg", "max", "enduranceScoreDTO") if k in es} or es
    return out


# --------------------------------------------------------------------------- #
# Coaching analysis
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=READ_ONLY)
@_errors
def review_week(date: str | None = None) -> dict[str, Any]:
    """Planned vs actual for a plan week: day-by-day status, volume, and the plan's 6-item weekly
    checklist (weekday runs easy, key run done, Leg A+B, ≥ 80% at HR ≤ 150, Tuesday strides,
    sleep ≥ 7 h on 5 nights). 5/6 = successful week. Includes next week's preview.

    Args:
        date: Any date in the week (default today).
    """
    return coach.review_week(_client, _plan(), _day(date))


@mcp.tool(annotations=READ_ONLY)
@_errors
def check_fatigue(date: str | None = None) -> dict[str, Any]:
    """Apply the plan's fatigue rules: easy-run HR drift at the same pace (last 3 vs previous 6 weeks),
    bad-sleep streaks, HRV status, Garmin readiness and ACWR. Verdict: proceed / easy_today / reduce_week_25."""
    return coach.fatigue_check(_client, _plan(), _day(date))


@mcp.tool(annotations=READ_ONLY)
@_errors
def analyze_test(test_id: str | None = None, activity_id: str | None = None) -> dict[str, Any]:
    """Evaluate a plan test: time trial (best effort over the distance, pass/target/stretch, Riegel HM
    equivalent, race pace from a 10K TT), monthly aerobic test (pace of the 30-min core at HR 143–147)
    or tempo test (20-min core at HR 160–165).

    Args:
        test_id: e.g. 'TT0', 'TT1', 'AER-2026-11', 'TMP-2026-12', 'RACE'. Optional if activity_id is on a test date.
        activity_id: The Garmin activity to evaluate (default: longest run on the test date).
    """
    return coach.analyze_test(_client, _plan(), test_id, activity_id)


@mcp.tool(annotations=READ_ONLY)
@_errors
def get_progress(date: str | None = None) -> dict[str, Any]:
    """All tests so far vs milestone targets (with month-over-month trend labels: improving / slowing /
    flat / regressing), upcoming tests, and planned-vs-actual weekly volume for the last 8 weeks."""
    return coach.progress(_client, _plan(), _day(date))


@mcp.tool(annotations=READ_ONLY)
@_errors
def get_race_outlook(date: str | None = None) -> dict[str, Any]:
    """Race outlook for JRF 2027: days to go, latest time-trial result and its HM equivalent, gap to
    the 2:00 target, Garmin race predictions, race-pace table and race-execution plan."""
    return coach.race_outlook(_client, _plan(), _day(date))


# --------------------------------------------------------------------------- #
# Write: workouts to Garmin Connect / watch
# --------------------------------------------------------------------------- #

def _upload(workout: dict[str, Any], day: dt.date, confirm: bool) -> dict[str, Any]:
    if not confirm:
        return {"preview": True, "date": day.isoformat(), "workout": workout,
                "next_step": "Call again with confirm=true to upload and schedule it on Garmin Connect."}
    res = _client.call("upload_workout", workout, cache=False)
    wid = res.get("workoutId") if isinstance(res, dict) else None
    if not wid:
        raise ToolError(f"Upload returned no workoutId: {res!r}")
    _client.call("schedule_workout", wid, day.isoformat(), cache=False)
    return {"uploaded": True, "workout_id": wid, "scheduled_for": day.isoformat(),
            "name": workout["workoutName"],
            "note": "Sync your watch (Garmin Connect app) – it appears under Training > Workouts and on the calendar."}


@mcp.tool(annotations=WRITE)
@_errors
def push_workout(date: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Build the planned session for a date as a structured Garmin workout (HR-targeted steps, repeats)
    and schedule it on the Garmin Connect calendar so it syncs to the watch.

    Args:
        date: YYYY-MM-DD / 'today' / 'tomorrow' (default today).
        confirm: false = preview only (default); true = upload + schedule.
    """
    plan = _plan()
    day = _day(date)
    dp = plan.day_plan(day)
    if dp is None:
        raise ToolError("Date is outside the plan.")
    if not parse_session(dp["session"]):
        return {"date": day.isoformat(), "message": "Rest day – nothing to push.", "strength": dp.get("strength")}
    wk = build_workout(plan, dp["session"], workout_name_for(dp), day,
                       description=f"{dp['structure']}. {dp.get('note') or ''}".strip())
    return _upload(wk, day, confirm)


@mcp.tool(annotations=WRITE)
@_errors
def push_custom_workout(session: str, date: str | None = None, name: str | None = None,
                        confirm: bool = False) -> dict[str, Any]:
    """Push an adapted session (e.g. after a fatigue verdict) as a structured workout.

    Args:
        session: Plan mini-language, segments joined by ' · ', e.g.
            '2km easy · 10km steady', '40min easy · 6x(20s strides / 60s easy)',
            '4km easy · 2x(3km tempo / 1km steady) · 4km easy'.
            Zones: recovery, easy, aerobic_test, steady, tempo, tempo_test, hmp, tt, race, strides.
        date: Day to schedule (default today).
        name: Workout name (default derived from the session).
        confirm: false = preview only; true = upload + schedule.
    """
    plan = _plan()
    day = _day(date)
    wk = build_workout(plan, session, name or f"Custom {day.strftime('%a %d %b')} · {session[:40]}", day)
    return _upload(wk, day, confirm)


@mcp.tool(annotations=WRITE)
@_errors
def push_week(date: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Push every remaining run of a plan week (from today on) as scheduled workouts.

    Args:
        date: Any date in the week (default today).
        confirm: false = preview list only; true = upload + schedule all.
    """
    plan = _plan()
    day = _day(date)
    idx = plan.week_index(day)
    if idx is None:
        raise ToolError("Date is outside the plan.")
    week = plan.resolve_week(idx)
    t = coach.today(plan)
    out = []
    for d in week["days"]:
        dd = dt.date.fromisoformat(d["date"])
        if dd < t or not parse_session(d["session"]):
            continue
        dp = plan.day_plan(dd)
        wk = build_workout(plan, d["session"], workout_name_for(dp), dd, description=d["structure"])
        if confirm:
            out.append(_upload(wk, dd, True))
        else:
            out.append({"date": d["date"], "name": wk["workoutName"], "structure": d["structure"],
                        "estimated_min": round(wk["estimatedDurationInSecs"] / 60)})
    return {"week": week["week"], "confirm": confirm, "workouts": out}


# --------------------------------------------------------------------------- #
# Prompts & resources
# --------------------------------------------------------------------------- #

@mcp.prompt()
def daily_checkin() -> str:
    """Morning check-in: what should I run today?"""
    return (
        "Call get_today. Tell me today's session in plain language (warm-up, main set, HR targets, "
        "estimated time, fueling if > 75 min), and adjust it if the readiness verdict says so. "
        "Keep it short. Offer to push it to my watch."
    )


@mcp.prompt()
def weekly_review_prompt() -> str:
    """Sunday-evening weekly review."""
    return (
        "Call review_week for this week and check_fatigue. Give me: checklist score with what passed/failed, "
        "planned vs actual km, the key run verdict (decoupling / tempo block), one thing to fix next week, "
        "and next week's plan. Apply the hold rule if two weeks in a row scored ≤ 3 (call review_week for last week too)."
    )


@mcp.prompt()
def monthly_progress_prompt() -> str:
    """Monthly milestone review."""
    return (
        "Call get_progress and get_race_outlook. Compare each test with its milestone, apply the plateau rules "
        "(improving / slowing / flat / regressing) and tell me whether to keep the schedule, progress faster, "
        "or back off. Finish with the current HM estimate vs the sub-2:00 target."
    )


@mcp.resource("plan://current", mime_type="application/x-yaml")
def plan_resource() -> str:
    """The full training plan YAML."""
    with open(_plan().path, encoding="utf-8") as fh:
        return fh.read()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Garmin coach MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.getenv("GARMIN_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_plan()  # fail fast on a broken plan file
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        from .http import serve

        serve(mcp, args.host, args.port)


if __name__ == "__main__":
    main()
