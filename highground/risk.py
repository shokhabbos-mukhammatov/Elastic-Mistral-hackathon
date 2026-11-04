"""Deterministic block risk. Pure: no I/O, no clock. Mistral explains this score; it never decides it.

score = S x H + C + coastal + trees
  S  susceptibility: 100-yr zone, sea-level-rise zone, the block's own storm-day 311 history
  H  hazard: rain vs SFPUC's "1.3 in in 3 h can already flood" rule, plus active NWS products
  C  confirmation: time-decayed 311 flood reports and CHP flooding incidents on/around the block
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

SFPUC_3H_MM = 33.0  # 1.3 in / 3 h (2024 Civil Grand Jury, SFPUC design storm)
LEVELS = ((3.5, "severe"), (2.0, "high"), (0.8, "elevated"), (0.0, "low"))
RANK = {"low": 0, "elevated": 1, "high": 2, "severe": 3}
NWS_BOOST = {"Flood Advisory": 0.3, "Flood Watch": 0.4, "Flash Flood Watch": 0.5, "Flood Warning": 1.0, "Flash Flood Warning": 1.5,
             "Coastal Flood Advisory": 0.0, "Coastal Flood Warning": 0.0, "High Wind Warning": 0.0, "Wind Advisory": 0.0}  # fmt: skip
FLOOD_CATS = {"flooding", "catch_basin", "sewer_backup"}


@dataclass
class Inputs:
    """Everything the engine may see about one block at one instant. Built by live or replay providers."""

    in_100yr: bool = False
    slr24: bool = False
    storm_reports_5y: int = 0  # flood reports on this block (k-ring 1) on storm days, 5 years before as_of
    rain_1h: float = 0.0  # mm observed, last hour
    rain_3h: float = 0.0  # mm observed, last 3 hours
    rain_next3h: float = 0.0  # mm forecast, next 3 hours (forecast as issued, never observed future)
    gust_kmh: float = 0.0
    nws: list[str] = field(default_factory=list)  # active product names
    tide_ft: float | None = None  # max observed/predicted water level next 3h, ft MLLW
    reports: list[tuple[str, float]] = field(default_factory=list)  # (category, age_hours) within k-ring 1, last 6h
    incidents_flooding: int = 0
    rain_point: str = ""
    sources: dict = field(default_factory=dict)  # reason code -> source label / url


@dataclass
class Risk:
    level: str
    score: float
    s: float
    h: float
    c: float
    reasons: list[dict]
    as_of: str


def level_for(score: float) -> str:
    return next(name for cut, name in LEVELS if score >= cut)


def assess(i: Inputs, as_of: datetime) -> Risk:
    reasons = []

    def why(code, text, value=None, unit=None):
        reasons.append({"code": code, "text": text, "value": value, "unit": unit, "source": i.sources.get(code)})

    s = 0.3
    if i.in_100yr:
        s += 0.6
        why("zone_100yr", "Inside the city's 100-year storm flood risk zone", True)
    if i.storm_reports_5y:
        s += min(0.9, 0.15 * math.log2(1 + i.storm_reports_5y))
        why("history", f"{i.storm_reports_5y} flood reports on this block during past storms (5 years)", i.storm_reports_5y, "reports")

    worst = max(i.rain_3h, i.rain_1h * 2, i.rain_next3h)
    h = worst / SFPUC_3H_MM
    if i.rain_3h >= 1:
        why("rain_3h", f"{i.rain_3h:.1f} mm of rain in the last 3 hours", round(i.rain_3h, 1), "mm")
    if i.rain_next3h >= 1:
        why("rain_next3h", f"{i.rain_next3h:.1f} mm of rain forecast in the next 3 hours", round(i.rain_next3h, 1), "mm")
    for ev in i.nws:
        b = NWS_BOOST.get(ev, 0.0)
        h += b
        why("nws", f"National Weather Service: {ev} in effect", ev)

    c = 0.0
    flood = [age for cat, age in i.reports if cat in FLOOD_CATS]
    if flood:
        c = min(2.0, 0.6 * sum(math.exp(-age / 3) for age in flood))
        why("reports", f"{len(flood)} flooding/drain reports on or next to this block in the last 6 hours", len(flood), "reports")
    if i.incidents_flooding:
        c += 0.5 * min(2, i.incidents_flooding)
        why("chp", f"{i.incidents_flooding} CHP road incident(s) mentioning flooding nearby", i.incidents_flooding)

    coastal = 0.0
    if i.slr24 and i.tide_ft is not None and i.tide_ft >= 6.5:
        coastal = min(1.0, (i.tide_ft - 6.5) * 1.0 + 0.3)
        why("tide", f"High tide of {i.tide_ft:.1f} ft at the Golden Gate gauge, block is in the shoreline flood zone", round(i.tide_ft, 1), "ft")
    trees = 0.0
    if i.gust_kmh >= 60:
        trees = 0.3
        why("wind", f"Wind gusts to {i.gust_kmh:.0f} km/h; falling trees and limbs are possible", round(i.gust_kmh), "km/h")
    if any(cat == "tree" for cat, _ in i.reports):
        trees += 0.2

    score = round(s * h + c + coastal + trees, 3)
    return Risk(level_for(score), score, round(s, 3), round(h, 3), round(c, 3), reasons, as_of.isoformat())


ACTIONS = {
    "low": ["Keep storm drains near you clear of leaves.", "Check the forecast before heavy rain."],
    "elevated": ["Clear leaves from the storm drain on your block.", "Move valuables off the floor of garages and basements.",
                 "Avoid parking in low spots and underpasses."],
    "high": ["Move your car out of the garage or low street before water rises.", "Put sandbags or barriers at doorways if your building floods.",
             "Do not walk or drive through standing water.", "Report flooding to SF311 (call 311 or use the app)."],
    "severe": ["Stay out of basements and underpasses now.", "Move people and valuables to higher floors.",
               "Never drive through floodwater: 6 inches can stall a car.", "Call 911 if anyone is in danger."],
}  # fmt: skip
HELP = [
    {"text": "SFPUC flood maps and how to prepare", "url": "https://www.sfpuc.gov/learning/emergency-preparedness/flood-maps"},
    {"text": "Report flooding or a clogged drain to SF311", "url": "https://sf311.org"},
    {"text": "Floodwater Grant Program", "url": "https://www.sfpuc.gov/floodwatergrant"},
    {"text": "Adopt a Drain near you", "url": "https://adoptadrain.sfwater.org/"},
    {"text": "Official alerts: AlertSF", "url": "https://www.sf.gov/alertsf"},
]  # fmt: skip
