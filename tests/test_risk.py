"""Unit tests for the deterministic risk engine: pure, no I/O, no clock, no look-ahead."""

import dataclasses
import json
import math
from datetime import UTC, datetime

import pytest

from highground.risk import LEVELS, RANK, SFPUC_3H_MM, Inputs, assess, level_for

T0 = datetime(2022, 12, 31, 18, 0, tzinfo=UTC)
T1 = datetime(2023, 6, 1, 9, 30, tzinfo=UTC)


def rich_inputs() -> Inputs:
    return Inputs(in_100yr=True, slr24=True, storm_reports_5y=4, rain_1h=8.0, rain_3h=40.0, rain_next3h=5.0, gust_kmh=65.0,
                  nws=["Flood Warning"], tide_ft=7.2, reports=[("flooding", 0.5), ("catch_basin", 2.0), ("tree", 1.0)], incidents_flooding=1,
                  rain_point="ne", sources={"rain_3h": "test-weather", "nws": "test-nws"})  # fmt: skip


# ---------------------------------------------------------------- purity / determinism


def test_assess_is_deterministic():
    i = rich_inputs()
    a, b = assess(i, T0), assess(i, T0)
    assert a == b
    assert a.reasons == b.reasons


def test_assess_does_not_mutate_inputs():
    i = rich_inputs()
    before = dataclasses.asdict(i)
    assess(i, T0)
    assess(i, T1)
    assert dataclasses.asdict(i) == before


def test_assess_uses_only_the_given_clock():
    i = rich_inputs()
    a, b = assess(i, T0), assess(i, T1)
    assert a.as_of == T0.isoformat() and b.as_of == T1.isoformat()
    assert a.score == b.score and a.level == b.level  # score never depends on the wall clock


# ---------------------------------------------------------------- levels


def test_dry_block_in_no_zone_is_low():
    r = assess(Inputs(), T0)
    assert r.level == "low"
    assert r.score == 0.0
    assert r.s == 0.3 and r.h == 0.0 and r.c == 0.0
    assert r.reasons == []


def test_heavy_rain_on_100yr_block_with_history_reaches_high():
    r = assess(Inputs(in_100yr=True, storm_reports_5y=10, rain_3h=50.0), T0)
    assert r.score >= 2.0
    assert RANK[r.level] >= RANK["high"]
    # same rain on a dry block outside every zone stays low: susceptibility drives the product S x H
    dry = assess(Inputs(rain_3h=50.0), T0)
    assert dry.level == "low"
    assert dry.score == pytest.approx(0.3 * 50.0 / SFPUC_3H_MM, abs=1e-3)


def test_flood_warning_raises_score_vs_no_product():
    base = Inputs(in_100yr=True, rain_3h=20.0)
    plain, warned = assess(base, T0), assess(dataclasses.replace(base, nws=["Flood Warning"]), T0)
    assert warned.score > plain.score
    assert warned.h == pytest.approx(plain.h + 1.0, abs=1e-3)
    assert any(r["code"] == "nws" and "Flood Warning" in r["text"] for r in warned.reasons)
    assert not any(r["code"] == "nws" for r in plain.reasons)


def test_nearby_flood_reports_raise_confirmation():
    none = assess(Inputs(), T0)
    one = assess(Inputs(reports=[("flooding", 1.0)]), T0)
    two = assess(Inputs(reports=[("flooding", 1.0), ("catch_basin", 0.5)]), T0)
    assert none.c == 0.0
    assert one.c == pytest.approx(0.6 * math.exp(-1.0 / 3), abs=1e-3)
    assert two.c > one.c > 0.0
    assert one.score > none.score
    # a non-flood report (tree) adds no confirmation
    assert assess(Inputs(reports=[("tree", 0.5)]), T0).c == 0.0
    # confirmation decays with age
    fresh = assess(Inputs(reports=[("flooding", 0.1)]), T0)
    old = assess(Inputs(reports=[("flooding", 5.0)]), T0)
    assert fresh.c > old.c


# ---------------------------------------------------------------- reasons carry codes + receipts-friendly numbers


def test_reasons_carry_codes_and_numbers():
    r = assess(rich_inputs(), T0)
    by_code = {x["code"]: x for x in r.reasons}
    assert set(by_code) == {"zone_100yr", "history", "rain_3h", "rain_next3h", "nws", "reports", "chp", "tide", "wind"}
    for reason in r.reasons:
        assert set(reason) == {"code", "text", "value", "unit", "source"}
    assert by_code["zone_100yr"]["value"] is True
    assert by_code["history"]["value"] == 4 and by_code["history"]["unit"] == "reports"
    assert by_code["rain_3h"]["value"] == 40.0 and by_code["rain_3h"]["unit"] == "mm"
    assert by_code["rain_next3h"]["value"] == 5.0 and by_code["rain_next3h"]["unit"] == "mm"
    assert by_code["nws"]["value"] == "Flood Warning"
    assert by_code["reports"]["value"] == 2 and by_code["reports"]["unit"] == "reports"
    assert by_code["chp"]["value"] == 1
    assert by_code["tide"]["value"] == 7.2 and by_code["tide"]["unit"] == "ft"
    assert by_code["wind"]["value"] == 65 and by_code["wind"]["unit"] == "km/h"
    # source labels from Inputs.sources ride along on the reason codes
    assert by_code["rain_3h"]["source"] == "test-weather"
    assert by_code["nws"]["source"] == "test-nws"
    assert by_code["zone_100yr"]["source"] is None


def test_reason_numbers_appear_in_the_facts_given_to_the_model():
    r = assess(rich_inputs(), T0)
    facts = {"level": r.level, "reasons": [{"text": x["text"], "value": x["value"], "unit": x["unit"]} for x in r.reasons]}
    blob = json.dumps(facts)
    for x in r.reasons:
        v = x["value"]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            assert str(v) in blob, f"{x['code']} value {v} is not a number the briefing could cite"


def test_score_is_the_documented_sum():
    i = rich_inputs()
    r = assess(i, T0)
    worst = max(i.rain_3h, i.rain_1h * 2, i.rain_next3h)
    s = 0.3 + 0.6 + min(0.9, 0.15 * math.log2(1 + i.storm_reports_5y))
    h = worst / SFPUC_3H_MM + 1.0  # Flood Warning
    c = min(2.0, 0.6 * (math.exp(-0.5 / 3) + math.exp(-2.0 / 3))) + 0.5 * min(2, i.incidents_flooding)
    coastal = min(1.0, (i.tide_ft - 6.5) + 0.3)
    trees = 0.3 + 0.2  # gust >= 60 km/h + a tree report
    assert r.score == pytest.approx(round(s * h + c + coastal + trees, 3), abs=1e-3)
    assert r.level == level_for(r.score)


# ---------------------------------------------------------------- level_for thresholds


@pytest.mark.parametrize("score,expected", [
    (0.0, "low"), (0.7999, "low"),
    (0.8, "elevated"), (1.9999, "elevated"),
    (2.0, "high"), (3.4999, "high"),
    (3.5, "severe"), (99.0, "severe"),
])
def test_level_for_thresholds(score, expected):
    assert level_for(score) == expected


def test_levels_are_ordered_and_match_rank():
    cuts = [c for c, _ in LEVELS]
    assert cuts == sorted(cuts, reverse=True)
    for cut, name in LEVELS:
        assert level_for(cut) == name
        assert RANK[name] == {"low": 0, "elevated": 1, "high": 2, "severe": 3}[name]
