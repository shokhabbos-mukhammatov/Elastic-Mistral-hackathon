"""Replay: synthetic dataset, strict no-look-ahead cutoffs, backtest shape. No network, no Elasticsearch."""

from datetime import UTC, datetime, timedelta

import pytest

from highground import geo
from highground.replay import Replay, backtest, day_bounds

DAY = "2022-12-31"

# three blocks (H3 res-9 cells) inside SF
C1 = geo.cell(37.7749, -122.4194)  # Civic Center
C2 = geo.cell(37.7694, -122.4862)  # SoMa
C3 = geo.cell(37.8080, -122.4650)  # Richmond
_lat, _lon = geo.center(C1)
NEIGHBOR = geo.cell(_lat + 0.002, _lon + 0.002)  # k-ring 1 of C1, same weather point
PT1 = geo.nearest_point(_lat, _lon)  # weather point serving C1

assert NEIGHBOR in geo.disk(C1, 1) and NEIGHBOR != C1
assert geo.nearest_point(*geo.center(NEIGHBOR)) == PT1

Z = lambda dt: dt.isoformat().replace("+00:00", "Z")  # IEM-style "Z" timestamps, as build_dataset stores them


def u(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2022, 12, 31, h, m, s, tzinfo=UTC)


def make_ds() -> dict:
    # hourly weather rows for every point in geo.POINTS, 06:00Z..22:00Z; unique rain amounts on PT1 so look-ahead is detectable
    rain_at = {14: 11.0, 15: 13.0, 16: 17.0, 18: 101.0}
    weather = []
    for name in geo.POINTS:
        for h in range(6, 23):
            weather.append({"@timestamp": Z(u(h)), "point": name, "rain_mm": rain_at.get(h, 0.0) if name == PT1 else 0.0,
                            "gust_kmh": 70.0 if (name == PT1 and h == 16) else 0.0})

    def rep(cat: str, c: str, at: datetime) -> dict:
        lat, lon = geo.center(c)
        return {"case_id": f"case-{cat}-{c[-6:]}-{at:%H%M%S}", "category": cat, "subcategory": cat, "requested_at": at.isoformat(),
                "location": {"lat": lat, "lon": lon}, "h3_9": c, "address": "1 Test St", "neighborhood": "Test", "status": "open"}

    reports = [
        rep("flooding", C1, u(17, 0, 0)),    # exactly 6 h before 23:00Z -> outside the window at 23:00Z
        rep("flooding", C1, u(17, 0, 1)),    # 1 s inside the window at 23:00Z
        rep("flooding", NEIGHBOR, u(17, 30)),
        rep("flooding", C1, u(18, 0, 0)),    # counts at t = 18:00Z exactly
        rep("flooding", C1, u(18, 30)),      # future at t = 18:00Z -> must never count there
        rep("flooding", C2, u(19, 0)),
        rep("tree", C3, u(20, 0)),
        rep("flooding", C1, u(9) + timedelta(days=1)),  # next day: outside the replay day
    ]
    return {
        "day": DAY,
        "built_at": Z(u(12)),
        "weather_source": "synthetic-test",
        "weather": weather,
        "reports": reports,
        "nws": [
            {"event": "Flood Warning", "valid_from": "2022-12-31T15:00:00Z", "valid_to": "2022-12-31T23:00:00Z",
             "issued_at": "2022-12-31T14:30:00Z", "url": "https://example.test/fw"},
            {"event": "Flood Advisory", "valid_from": "2022-12-31T21:00:00Z", "valid_to": "2022-12-31T23:00:00Z",
             "issued_at": "2022-12-31T20:00:00Z", "url": "https://example.test/fa"},
        ],
        "tide": [{"t": "2022-12-31T16:00:00Z", "ft": 5.0}, {"t": "2022-12-31T17:00:00Z", "ft": 6.9}, {"t": "2022-12-31T18:00:00Z", "ft": 7.1}],
        "profiles": {
            C1: {"h3_9": C1, "in_100yr": True, "slr24": False, "storm_reports_5y": 5, "neighborhood": "Civic Center"},
            C2: {"h3_9": C2, "in_100yr": False, "slr24": True, "storm_reports_5y": 0, "neighborhood": "SoMa"},
            C3: {"h3_9": C3, "in_100yr": False, "slr24": False, "storm_reports_5y": 2, "neighborhood": "Richmond"},
        },
        "storm_days_before": 1,
    }


@pytest.fixture(scope="module")
def ds() -> dict:
    return make_ds()


@pytest.fixture(scope="module")
def rp(ds) -> Replay:
    return Replay(ds)


def test_day_bounds():
    start, end = day_bounds(DAY)
    assert (start, end) == (u(8), u(8) + timedelta(days=1))  # 2022-12-31 00:00 PT == 08:00Z


def test_cells_cover_profiles_and_flood_reports(rp):
    assert {C1, C2, C3, NEIGHBOR} <= set(rp.cells())


def test_inputs_carry_profile_nearest_point_and_sources(rp):
    i = rp.inputs(C1, u(18))
    assert i.in_100yr is True and i.storm_reports_5y == 5 and i.slr24 is False
    assert i.rain_point == PT1 == geo.nearest_point(*geo.center(C1))
    assert set(i.sources) >= {"rain_3h", "nws", "reports", "tide", "history", "zone_100yr"}
    i2 = rp.inputs(C2, u(18))
    assert i2.slr24 is True and i2.in_100yr is False and i2.storm_reports_5y == 0


# ---------------------------------------------------------------- no look-ahead


def test_rain_hour_counts_only_once_it_has_ended(rp):
    # an hourly row labeled ts is observed only once ts + 1 h <= t
    assert rp.inputs(C1, u(16, 30)).rain_3h == 24.0   # 14:00 (11) + 15:00 (13); the 16:00 hour ends at 17:00, not yet seen
    assert rp.inputs(C1, u(17, 0)).rain_3h == 41.0    # the 16:00 hour (17) has now ended
    assert rp.inputs(C1, u(17, 0)).rain_1h == 17.0
    # the 101 mm hour starting 18:00Z ends at 19:00Z and must not leak into any earlier t
    assert rp.inputs(C1, u(18, 30)).rain_3h == 30.0   # 15:00 (13) + 16:00 (17) only
    assert rp.inputs(C1, u(18, 59)).rain_3h == 30.0
    assert rp.inputs(C1, u(19, 0)).rain_3h == 118.0   # 16:00 (17) + 18:00 (101)


def test_no_rain_hour_ending_after_t_is_ever_counted(rp):
    start, end = day_bounds(DAY)
    t = start
    while t < end:
        for c in (C1, C2, C3, NEIGHBOR):
            i = rp.inputs(c, t)
            point_rows = rp.rain[i.rain_point]
            # independent restatement of the cutoff: only hours that have ended by t, inside the 3 h window
            expect3 = sum(mm for ts, mm, _ in point_rows if ts + timedelta(hours=1) <= t and ts >= t - timedelta(hours=3, minutes=59))
            expect1 = sum(mm for ts, mm, _ in point_rows if ts + timedelta(hours=1) <= t and ts >= t - timedelta(hours=1, minutes=59))
            assert i.rain_3h == expect3 and i.rain_1h == expect1
            assert all(age >= 0.0 for _, age in i.reports), f"future report counted at {t} for {c}"
        t += timedelta(minutes=15)


def test_reports_never_count_before_they_exist(rp):
    at_1800 = rp.inputs(C1, u(18, 0)).reports
    ages = sorted(age for _, age in at_1800)
    assert len(at_1800) == 4                       # 17:00:00, 17:00:01, 17:30 (neighbor), 18:00
    assert ages[0] == 0.0                          # a report filed exactly at t counts
    assert all(0.0 <= a < 6.0 for a in ages)
    # at 17:59Z the 18:00Z report must not be there yet (it would show up as a negative age)
    at_1759 = rp.inputs(C1, u(17, 59)).reports
    assert at_1759 and all(age > 0.4 for _, age in at_1759)


def test_report_six_hour_window_is_strict(rp):
    ages = [age for _, age in rp.inputs(C1, u(23, 0)).reports]
    assert len(ages) == 4                          # 17:00:01, 17:30, 18:00, 18:30; the 17:00:00 report is exactly 6 h old -> dropped
    assert all(a < 6.0 for a in ages)
    assert any(abs(a - (6.0 - 1 / 3600)) < 1e-6 for a in ages)  # the 17:00:01 report is 1 s inside the window


def test_nws_product_counts_only_once_issued_and_while_valid(rp):
    assert rp.inputs(C1, u(14, 0)).nws == []                      # not issued yet (issued 14:30Z)
    assert rp.inputs(C1, u(14, 30)).nws == ["Flood Warning"]
    assert rp.inputs(C1, u(15, 0)).nws == ["Flood Warning"]
    assert rp.inputs(C1, u(19, 59)).nws == ["Flood Warning"]      # advisory issued 20:00Z is not out yet
    assert rp.inputs(C1, u(20, 0)).nws == ["Flood Advisory", "Flood Warning"]
    assert rp.inputs(C1, u(23, 0)).nws == []                      # valid_to reached


def test_tide_uses_only_hours_up_to_t(rp):
    assert rp.inputs(C1, u(15, 0)).tide_ft is None
    assert rp.inputs(C1, u(16, 30)).tide_ft == 5.0
    assert rp.inputs(C1, u(17, 30)).tide_ft == 6.9
    assert rp.inputs(C1, u(18, 0)).tide_ft == 7.1


def test_frame_reports_respect_the_cutoff(rp):
    f = rp.frame(u(18, 0))
    assert set(f) == {"t", "cells", "reports"}
    assert f["t"] == u(18).isoformat()
    assert {r["h3_9"] for r in f["reports"]} == {C1, NEIGHBOR}
    assert all(r["at"] <= f["t"] for r in f["reports"])
    assert isinstance(f["cells"], dict)


# ---------------------------------------------------------------- backtest

BACKTEST_KEYS = {"day", "threshold", "blocks_with_flood_reports", "flagged_before_or_at_report", "flagged_after_report", "never_flagged",
                 "median_lead_min", "flagged_blocks", "false_alarm_blocks", "first_flag", "first_report", "weather_source"}


def test_backtest_keys_and_counts(ds):
    out = backtest(ds, step_min=60, threshold="high")
    assert set(out) == BACKTEST_KEYS
    assert out["day"] == DAY and out["threshold"] == "high" and out["weather_source"] == "synthetic-test"
    # the three cells with flood reports inside the replay day (the next-day report is excluded)
    assert out["blocks_with_flood_reports"] == 3
    assert out["flagged_before_or_at_report"] + out["flagged_after_report"] + out["never_flagged"] == out["blocks_with_flood_reports"]
    # C1 (100-yr zone, storm history) flags at 15:00Z when the Flood Warning is issued + first rain hour ends; NEIGHBOR flags at 18:00Z
    assert out["flagged_blocks"] == 2
    assert out["flagged_before_or_at_report"] == 1   # C1: flagged 15:00Z, first report 17:00Z -> +120 min lead
    assert out["flagged_after_report"] == 1           # NEIGHBOR: flagged 18:00Z, its report came at 17:30Z
    assert out["never_flagged"] == 1                  # C2 only ever reaches "elevated"
    assert out["false_alarm_blocks"] == 0
    assert out["median_lead_min"] == 120.0
    assert datetime.fromisoformat(out["first_flag"]) == u(15, 0)
    assert datetime.fromisoformat(out["first_report"]) == u(17, 0)


def test_backtest_is_deterministic(ds):
    assert backtest(ds, step_min=60) == backtest(ds, step_min=60)
