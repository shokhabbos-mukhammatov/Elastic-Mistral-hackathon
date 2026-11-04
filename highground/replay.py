"""Storm replay + backtest with no look-ahead, and the per-block history profiles both live and replay use.

A replay dataset is a JSON snapshot of real archived inputs for one Pacific-time day:
  311 reports (DataSF, true request timestamps), observed hourly rain + gusts (Open-Meteo archive at 5 SF points,
  IEM ASOS SFO fallback), NWS products as issued (IEM VTEC archive), observed tide (NOAA 9414290), and block profiles
  computed only from the 5 years *before* that day.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from highground import feeds, geo
from highground.risk import RANK, Inputs, assess

PT = ZoneInfo("America/Los_Angeles")
DIR = Path(__file__).resolve().parent.parent / "data" / "replay"
STORM_DAY_MIN = 40  # citywide flood/drain reports in one day that make it a "storm day"
FLOOD = ["flooding", "catch_basin"]


def day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(day).replace(tzinfo=PT)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


# ---------------------------------------------------------------- profiles (ES aggregations + geo_shape msearch)


def storm_days(s, start: datetime, end: datetime) -> list[str]:
    r = s.search("reports", size=0, query={"bool": {"filter": [{"terms": {"category": FLOOD}}, {"range": {"requested_at": {"gte": start.isoformat(), "lt": end.isoformat()}}}]}},
                 aggs={"d": {"date_histogram": {"field": "requested_at", "calendar_interval": "day", "time_zone": "America/Los_Angeles", "min_doc_count": STORM_DAY_MIN}}})  # fmt: skip
    return [b["key_as_string"][:10] for b in r["aggregations"]["d"]["buckets"]]


def zone_flags(s, cells: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(cells), 200):
        chunk, body = cells[i : i + 200], []
        for c in chunk:
            lat, lon = geo.center(c)
            body += [{"index": s.ix("zones")}, {"size": 0, "query": {"geo_shape": {"geometry": {"shape": {"type": "point", "coordinates": [lon, lat]}, "relation": "intersects"}}},
                                                "aggs": {"l": {"terms": {"field": "layer"}}, "n": {"terms": {"field": "name", "size": 1, "exclude": ["100yr", "slr24"]}}}}]  # fmt: skip
        for c, r in zip(chunk, s.es.msearch(searches=body)["responses"], strict=False):
            layers = {b["key"] for b in r.get("aggregations", {}).get("l", {}).get("buckets", [])}
            nb = r.get("aggregations", {}).get("n", {}).get("buckets", [])
            out[c] = {"in_100yr": "100yr" in layers, "slr24": "slr24" in layers, "neighborhood": nb[0]["key"] if nb else None}
    return out


def build_profiles(s, as_of: datetime, index: str | None = None) -> dict:
    """Per-cell storm history from the 5 years before as_of only (so a replay never sees its own storm)."""
    start = as_of - timedelta(days=5 * 365)
    days = storm_days(s, start, as_of)
    counts: dict[str, int] = defaultdict(int)
    if days:
        should = [{"range": {"requested_at": {"gte": f"{d}T00:00:00", "lt": f"{d}T23:59:59", "time_zone": "America/Los_Angeles"}}} for d in days]
        r = s.search("reports", size=0, query={"bool": {"filter": [{"terms": {"category": FLOOD}}, {"bool": {"should": should, "minimum_should_match": 1}}]}},
                     aggs={"c": {"terms": {"field": "h3_9", "size": 20000}}})  # fmt: skip
        counts.update({b["key"]: b["doc_count"] for b in r["aggregations"]["c"]["buckets"]})
    flood_cells = s.search("reports", size=0, query={"term": {"zone_100yr": True}}, aggs={"c": {"terms": {"field": "h3_9", "size": 20000}}})["aggregations"]["c"]["buckets"]
    cells = sorted(set(counts) | {b["key"] for b in flood_cells})
    flags = zone_flags(s, cells)
    profiles = {}
    for c in cells:
        ring = sum(counts.get(n, 0) for n in geo.disk(c, 1))
        lat, lon = geo.center(c)
        profiles[c] = {"h3_9": c, "storm_reports_5y": ring, "storm_days_5y": len(days), **flags[c], "centroid": {"lat": lat, "lon": lon}}
    if index:
        s.write(index, [{"_id": c, **p} for c, p in profiles.items()], refresh=True)
    return {"cells": len(profiles), "storm_days": len(days), "profiles": profiles if not index else None}


# ---------------------------------------------------------------- dataset (real archived inputs, fetched once)


def _weather(http, day: str) -> tuple[list[dict], str]:
    p = {"latitude": ",".join(str(v[0]) for v in geo.POINTS.values()), "longitude": ",".join(str(v[1]) for v in geo.POINTS.values()),
         "hourly": "precipitation,wind_gusts_10m", "timezone": "UTC", "start_date": (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat(),
         "end_date": (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()}  # fmt: skip
    r = http.get("https://archive-api.open-meteo.com/v1/archive", params=p, timeout=60)
    if r.status_code == 200:
        return feeds.weather_docs(r.json(), None, tag=f"replay{day}", source="open-meteo-archive", replay=day), "Open-Meteo ERA5 archive (5 SF points)"
    # fallback: SFO airport gauge (IEM ASOS), applied to every point
    a = http.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", params={"station": "SFO", "data": "p01i", "tz": "Etc/UTC", "format": "onlycomma",
                 "year1": p["start_date"][:4], "month1": p["start_date"][5:7], "day1": p["start_date"][8:], "year2": p["end_date"][:4], "month2": p["end_date"][5:7],
                 "day2": p["end_date"][8:], "report_type": "3"}, timeout=60).text  # fmt: skip
    hourly: dict[str, float] = defaultdict(float)
    for line in a.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[2] not in ("M", "T", ""):
            hourly[parts[1][:13]] = max(hourly[parts[1][:13]], float(parts[2]) * 25.4)
    out = []
    for name in geo.POINTS:
        for h, mm in hourly.items():
            out.append({"@timestamp": datetime.fromisoformat(h + ":00").replace(tzinfo=UTC).isoformat(), "kind": "weather", "point": name, "rain_mm": mm, "gust_kmh": None})
    return out, "IEM ASOS SFO airport gauge (Open-Meteo unavailable)"


def build_dataset(s, http, day: str) -> dict:
    start, end = day_bounds(day)
    reports = [d for d in feeds.fetch_datasf(http, start - timedelta(hours=6), end + timedelta(hours=6), services=("Sewer Issues", "Sewer", "Catch Basin Maintenance", "Tree Maintenance"))]
    weather, wsrc = _weather(http, day)
    y = day[:4]
    iem = http.get("https://mesonet.agron.iastate.edu/json/vtec_events_bywfo.py", params={"wfo": "MTR", "year": y}, timeout=60).json()
    nws = feeds.iem_docs(iem, (start - timedelta(days=2)).isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"), day)
    tide = http.get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", params={"station": "9414290", "product": "hourly_height", "datum": "MLLW",
                    "units": "english", "time_zone": "gmt", "format": "json", "begin_date": (start - timedelta(hours=6)).strftime("%Y%m%d %H:%M"),
                    "end_date": (end + timedelta(hours=6)).strftime("%Y%m%d %H:%M")}, timeout=60).json()  # fmt: skip
    prof = build_profiles(s, start)
    ds = {"day": day, "built_at": datetime.now(UTC).isoformat(), "weather_source": wsrc,
          "reports": [{k: r.get(k) for k in ("case_id", "category", "subcategory", "requested_at", "location", "h3_9", "address", "neighborhood", "status")} for r in reports],
          "weather": [{k: w.get(k) for k in ("@timestamp", "point", "rain_mm", "gust_kmh")} for w in weather],
          "nws": [{k: n.get(k) for k in ("event", "valid_from", "valid_to", "issued_at", "url")} for n in nws],
          "tide": [{"t": t["@timestamp"], "ft": t["value"]} for t in feeds.tide_docs(tide, "tide_obs")],
          "profiles": prof["profiles"], "storm_days_before": prof["storm_days"]}  # fmt: skip
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{day}.json").write_text(json.dumps(ds))
    return {"day": day, "reports": len(ds["reports"]), "weather": len(ds["weather"]), "nws": len(ds["nws"]), "tide": len(ds["tide"]), "cells": len(ds["profiles"]), "weather_source": wsrc}


def load(day: str) -> dict:
    return json.loads((DIR / f"{day}.json").read_text())


# ---------------------------------------------------------------- as-of inputs (strict cutoff = no look-ahead)


class Replay:
    def __init__(self, ds: dict):
        self.ds = ds
        self.rain: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
        for w in ds["weather"]:
            self.rain[w["point"]].append((datetime.fromisoformat(w["@timestamp"]), w["rain_mm"] or 0.0, w["gust_kmh"] or 0.0))
        self.reports = sorted(((datetime.fromisoformat(r["requested_at"]), r) for r in ds["reports"]), key=lambda x: x[0])
        self.tide = [(datetime.fromisoformat(t["t"]), t["ft"]) for t in ds["tide"]]

    def inputs(self, c: str, t: datetime, active_cells: dict | None = None) -> Inputs:
        p = self.ds["profiles"].get(c, {})
        lat, lon = geo.center(c)
        pt = geo.nearest_point(lat, lon)
        # hourly totals are labeled by hour start; an hour counts as observed only once it has ended
        obs = [(ts, mm, g) for ts, mm, g in self.rain[pt] if ts + timedelta(hours=1) <= t]
        r1 = sum(mm for ts, mm, _ in obs if ts >= t - timedelta(hours=1, minutes=59))
        r3 = sum(mm for ts, mm, _ in obs if ts >= t - timedelta(hours=3, minutes=59))
        gust = max((g for ts, _, g in obs if ts >= t - timedelta(hours=3)), default=0.0)
        z = lambda d: d.isoformat().replace("+00:00", "Z")
        # a product counts once issued, while valid, or if it starts within 12 h (a watch issued ahead of the storm)
        nws = sorted({n["event"] for n in self.ds["nws"] if n["issued_at"] <= z(t) < n["valid_to"] and n["valid_from"] <= z(t + timedelta(hours=12))})
        ring = set(geo.disk(c, 1))
        near = [(r["category"], (t - ts).total_seconds() / 3600) for ts, r in self.reports if ts <= t and ts > t - timedelta(hours=6) and r["h3_9"] in ring]
        tide = max((ft for ts, ft in self.tide if t - timedelta(hours=1) <= ts <= t), default=None)
        return Inputs(in_100yr=p.get("in_100yr", False), slr24=p.get("slr24", False), storm_reports_5y=p.get("storm_reports_5y", 0), rain_1h=r1, rain_3h=r3,
                      gust_kmh=gust, nws=nws, tide_ft=tide, reports=near, rain_point=pt,
                      sources={"rain_3h": self.ds["weather_source"], "nws": "NWS via IEM VTEC archive", "reports": "DataSF 311 (vw6y-z8j6)",
                               "tide": "NOAA 9414290", "history": "DataSF 311, 5 years before this day", "zone_100yr": "DataSF jzu3-4yxp"})  # fmt: skip

    def cells(self) -> list[str]:
        return sorted(set(self.ds["profiles"]) | {r["h3_9"] for _, r in self.reports if r["category"] in FLOOD})

    def frame(self, t: datetime) -> dict:
        out = {}
        for c in self.cells():
            r = assess(self.inputs(c, t), t)
            if r.level != "low":
                out[c] = {"level": r.level, "score": r.score}
        rep = [{"h3_9": r["h3_9"], "category": r["category"], "at": ts.isoformat(), "lat": r["location"]["lat"], "lon": r["location"]["lon"]}
               for ts, r in self.reports if ts <= t and ts > t - timedelta(hours=6)]  # fmt: skip
        return {"t": t.isoformat(), "cells": out, "reports": rep}


def backtest(ds: dict, step_min: int = 15, threshold: str = "high") -> dict:
    """Lead time = first report on/next to a block minus the first time the engine flagged it >= threshold (as of then)."""
    rp = Replay(ds)
    start, end = day_bounds(ds["day"])
    cells = rp.cells()
    first_flag: dict[str, datetime] = {}
    t = start
    while t < end:
        for c in cells:
            if c not in first_flag and RANK[assess(rp.inputs(c, t), t).level] >= RANK[threshold]:
                first_flag[c] = t
        t += timedelta(minutes=step_min)
    first_report: dict[str, datetime] = {}
    for ts, r in rp.reports:
        if start <= ts < end and r["category"] in FLOOD:
            for n in geo.disk(r["h3_9"], 1):
                first_report.setdefault(n, ts)
    hit_cells = {r["h3_9"] for ts, r in rp.reports if start <= ts < end and r["category"] in FLOOD}
    leads = []
    for c in hit_cells:
        own = min(ts for ts, r in rp.reports if r["h3_9"] == c and r["category"] in FLOOD and start <= ts < end)
        if c in first_flag:
            leads.append((own - first_flag[c]).total_seconds() / 60)
    flagged_ok = sum(1 for c in first_flag if c in first_report and first_report[c] <= first_flag[c] + timedelta(hours=6))
    leads.sort()
    return {"day": ds["day"], "threshold": threshold, "blocks_with_flood_reports": len(hit_cells), "flagged_before_or_at_report": sum(1 for x in leads if x >= 0),
            "flagged_after_report": sum(1 for x in leads if x < 0), "never_flagged": len(hit_cells) - len(leads),
            "median_lead_min": leads[len(leads) // 2] if leads else None, "flagged_blocks": len(first_flag),
            "false_alarm_blocks": len(first_flag) - flagged_ok, "first_flag": min(first_flag.values()).isoformat() if first_flag else None,
            "first_report": min(first_report.values()).isoformat() if first_report else None, "weather_source": ds["weather_source"]}  # fmt: skip
