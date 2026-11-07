import datetime as dt
from unittest import mock

from garmin_mcp import coach
from garmin_mcp.workouts import build_workout
from conftest import activity


def _week0(fake):
    # Tue easy 40' + strides, Thu easy 45', Sat steady LR 15 km, Sun 5 km easy, Mon/Wed strength
    tue = [(2400, 2.4, 144)] + [(20, 4.5, 160), (60, 2.2, 148)] * 6
    fake.add(*activity(1, "2026-11-03", tue))
    fake.add(*activity(2, "2026-11-05", [(2700, 2.4, 145)]))
    fake.add(*activity(3, "2026-11-07", [(800, 2.5, 145), (4700, 2.75, 152)]))
    fake.add(*activity(4, "2026-11-08", [(2000, 2.5, 140)]))
    fake.add(*activity(5, "2026-11-02", [(1800, 0, 100)], type_key="strength_training", time="19:00:00"))
    fake.add(*activity(6, "2026-11-04", [(1800, 0, 100)], type_key="strength_training", time="19:00:00"))
    for i in range(7):
        fake.sleep[(dt.date(2026, 11, 2) + dt.timedelta(days=i)).isoformat()] = 7.4 if i != 3 else 6.0


def test_review_week_scores_full_week(fake, plan):
    _week0(fake)
    with mock.patch.object(coach, "today", return_value=dt.date(2026, 11, 8)):
        r = coach.review_week(fake, plan, dt.date(2026, 11, 8))
    assert r["complete"]
    c = r["checklist"]
    assert c["weekday_runs"]["ok"] and c["key_run"]["ok"] and c["legs"]["ok"]
    assert c["strides"]["ok"] and c["sleep"]["ok"]
    assert r["week_successful"]
    assert r["volume"]["actual_km"] > 25


def test_review_week_missed_key_run(fake, plan):
    fake.add(*activity(2, "2026-11-05", [(2700, 2.4, 145)]))
    with mock.patch.object(coach, "today", return_value=dt.date(2026, 11, 8)):
        r = coach.review_week(fake, plan, dt.date(2026, 11, 8))
    assert r["checklist"]["key_run"]["ok"] is False
    assert any(d["status"] == "missed" for d in r["days"])


def test_analyze_activity_steady_lr(fake, plan):
    _week0(fake)
    r = coach.analyze_activity(fake, plan, "3")
    assert r["planned"]["key_type"] == "steady_lr"
    assert "decoupling_pct" in r["aerobic_decoupling"]
    assert any("Decoupling" in n for n in r["assessment"])


def test_time_trial_result(fake, plan):
    # TT0 on 21 Nov: 2 km warmup, 5 km at 6:00/km, 2 km cooldown
    fake.add(*activity(10, "2026-11-21", [(800, 2.5, 140), (1800, 1000 / 360, 172), (800, 2.5, 150)]))
    r = coach.analyze_test(fake, plan, "TT0", None)
    assert r["result"]["avg_pace"] in ("5:59", "6:00")
    assert r["achieved"] == "baseline recorded"
    assert "half_marathon_equivalent_riegel" in r


def test_aerobic_test(fake, plan):
    fake.add(*activity(11, "2026-12-03", [(600, 2.3, 138), (1800, 1000 / 410, 145), (300, 2.3, 140)]))
    r = coach.analyze_test(fake, plan, None, "11")
    assert r["test_id"] == "AER-2026-11"
    assert r["core"]["avg_pace"] in ("6:50", "6:51", "6:49")
    assert r["verdict"] == "on/ahead of target"


def test_fatigue_flags_bad_sleep(fake, plan):
    for i in range(7):
        fake.sleep[(dt.date(2026, 11, 8) - dt.timedelta(days=i)).isoformat()] = 5.5 if i < 3 else 7.5
    r = coach.fatigue_check(fake, plan, dt.date(2026, 11, 8))
    assert r["verdict"] == "reduce_week_25"


def test_today_plan(fake, plan):
    r = coach.today_plan(fake, plan, dt.date(2026, 11, 7))
    assert r["plan"]["key_type"] == "steady_lr"
    assert r["readiness"]["verdict"] == "proceed"
    assert r["tomorrow"]["planned_km"] == 5


def test_build_workout_structure(plan):
    wk = build_workout(plan, "4km easy · 2x(3km tempo / 1km steady) · 4km easy", "t", dt.date(2026, 11, 14))
    steps = wk["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[1]["type"] == "RepeatGroupDTO" and steps[1]["numberOfIterations"] == 2
    tempo = steps[1]["workoutSteps"][0]
    assert tempo["targetValueOne"] == 158 and tempo["targetValueTwo"] == 165
    assert tempo["endCondition"]["conditionTypeKey"] == "distance" and tempo["endConditionValue"] == 3000
    assert steps[-1]["stepType"]["stepTypeKey"] == "cooldown"
    orders = [steps[0]["stepOrder"], steps[1]["stepOrder"], *[s["stepOrder"] for s in steps[1]["workoutSteps"]], steps[2]["stepOrder"]]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)


def test_hmp_uses_pace_target_when_set(plan):
    plan.race_pace_s_per_km = 335
    wk = build_workout(plan, "2km easy · 10km hmp", "t", dt.date(2027, 9, 1))
    hmp = wk["workoutSegments"][0]["workoutSteps"][1]
    assert hmp["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert hmp["targetValueOne"] < hmp["targetValueTwo"]
