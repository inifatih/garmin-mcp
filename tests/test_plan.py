import datetime as dt

import pytest

from garmin_mcp.plan import Repeat, Step, flatten, parse_session, pace_to_seconds, seconds_to_pace


def test_parse_simple_and_repeat():
    segs = parse_session("4km easy · 2x(3km tempo / 1km steady) · 4km easy")
    assert isinstance(segs[0], Step) and segs[0].km == 4 and segs[0].zone == "easy"
    assert isinstance(segs[1], Repeat) and segs[1].times == 2
    steps = flatten(segs)
    assert sum(s.km for s in steps) == 15  # last recovery skipped


def test_parse_time_units():
    segs = parse_session("40min easy · 6x(20s strides / 60s easy)")
    assert segs[0].seconds == 2400
    assert segs[1].work.seconds == 20 and segs[1].recovery.seconds == 60


def test_rest_and_errors():
    assert parse_session("rest") == []
    with pytest.raises(ValueError):
        parse_session("lots of running")


def test_pace_helpers():
    assert pace_to_seconds("6:15") == 375
    assert seconds_to_pace(375) == "6:15"


def test_plan_calendar(plan):
    assert plan.start_date == dt.date(2026, 11, 2)
    assert len(plan.weeks) == 51
    assert plan.week_index(dt.date(2026, 11, 7)) == 0
    assert plan.week_index(dt.date(2027, 10, 24)) == 50
    assert plan.week_index(dt.date(2027, 10, 25)) is None


def test_today_is_steady_long_run(plan):
    dp = plan.day_plan(dt.date(2026, 11, 7))
    assert dp["role"] == "key_run" and dp["key_type"] == "steady_lr"
    assert dp["planned_km"] == 15


def test_test_days_override_sessions(plan):
    tt0 = plan.day_plan(dt.date(2026, 11, 21))
    assert tt0["test"]["id"] == "TT0" and "5km tt" in tt0["session"]
    aer = plan.day_plan(dt.date(2026, 12, 3))
    assert aer["test"]["kind"] == "aerobic" and aer["role"] == "weekday_easy"
    race = plan.day_plan(dt.date(2027, 10, 24))
    assert race["role"] == "key_run" and race["test"]["id"] == "RACE"


def test_weekly_totals_match_plan_table(plan):
    for i in range(len(plan.weeks)):
        w = plan.resolve_week(i)
        total = sum(d["planned_km"] for d in w["days"])
        assert abs(total - w["planned_total_km"]) <= 3, (i, total, w["planned_total_km"])


def test_ramadan_has_no_long_run(plan):
    w = plan.resolve_week(15)
    assert max(d["planned_km"] for d in w["days"]) <= 8
    assert sum(1 for d in w["days"] if d["planned_km"]) == 5


def test_strength_stops_before_race(plan):
    w = plan.resolve_week(50)
    assert not any(d.get("strength") for d in w["days"])
