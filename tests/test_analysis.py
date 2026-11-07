from garmin_mcp import analysis as A
from conftest import make_details


def test_hr_distribution_and_cap(plan):
    s = A.samples_from_details(make_details([(1800, 2.4, 145), (600, 2.6, 160)]))
    d = A.hr_time_distribution(s, plan.zones)
    assert 0.74 <= d["share_at_or_below_easy_cap"] <= 0.76
    assert d["minutes_by_zone"]["tempo"] > 9


def test_decoupling_detects_drift():
    flat = A.samples_from_details(make_details([(3600, 2.6, 150)]))
    assert abs(A.aerobic_decoupling(flat)["decoupling_pct"]) < 0.5
    drift = A.samples_from_details(make_details([(2100, 2.6, 148), (2100, 2.6, 160)]))
    assert A.aerobic_decoupling(drift)["decoupling_pct"] > 5


def test_tempo_block_detection():
    s = A.samples_from_details(make_details([(1200, 2.4, 145), (1800, 2.75, 162), (900, 2.4, 148)]))
    blk = A.detect_hr_block(s, 158)
    assert blk is not None
    assert 4.6 <= blk["distance_km"] <= 5.2


def test_strides_counted():
    segs = [(1500, 2.4, 145)]
    for _ in range(6):
        segs += [(20, 4.5, 160), (60, 2.2, 150)]
    s = A.samples_from_details(make_details(segs))
    assert A.count_strides(s) == 6


def test_best_effort_finds_tt():
    segs = [(720, 2.4, 140), (1500, 1000 / 300, 175), (720, 2.4, 150)]  # 5k at 5:00/km
    s = A.samples_from_details(make_details(segs))
    be = A.best_effort(s, 5000)
    assert be["avg_pace"] in ("4:59", "5:00")


def test_riegel():
    assert round(A.riegel(3240, 10, 21.0975)) in range(7140, 7200)  # 54:00 10K ≈ 1:59


def test_hr_drift():
    runs = []
    for i in range(8):
        runs.append({"startTimeLocal": f"2026-11-{i + 1:02d} 05:00", "averageHR": 145, "averageSpeed": 2.4,
                     "movingDuration": 2700, "activityId": i})
    for i in range(3):
        runs.append({"startTimeLocal": f"2026-11-{i + 20:02d} 05:00", "averageHR": 152, "averageSpeed": 2.4,
                     "movingDuration": 2700, "activityId": 100 + i})
    eff = A.easy_run_efficiency(runs, easy_cap=155)
    d = A.hr_drift_vs_baseline(eff)
    assert d["implied_hr_change_bpm_at_same_pace"] >= 5


def test_monthly_classification():
    assert A.classify_monthly_change(420, 413) == "improving"
    assert A.classify_monthly_change(420, 418) == "flat"
    assert A.classify_monthly_change(420, 416) == "slowing"
    assert A.classify_monthly_change(420, 426) == "regressing"
