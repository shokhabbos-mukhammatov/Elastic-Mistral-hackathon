"""Live + historical feeds, normalized to Elasticsearch docs. One runner handles watermarks and failures.

SF Open311 (real-time 311), DataSF 311 (nightly history, authoritative subtypes), NWS alerts, NOAA tides,
Open-Meteo rain/gusts, CHP road incidents. All timestamps stored as UTC.
"""

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from highground.geo import POINTS, cell, in_sf

PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "HighGround-SF/hacknight"}
SF_UGC = ("CAZ006", "CAZ508", "CAZ509", "CAC075")  # NWS zones: SF city, Bay shoreline, Peninsula coast, county

_FLOOD = re.compile(r"\b(flood\w*|standing water|pond\w*|puddl\w*|underwater|inundat\w*|overflow\w*|water (is )?(rising|pooling|over))\b", re.IGNORECASE)
_DRAIN = re.compile(r"\b(catch ?basin|storm ?drain|drain\w*|clogged|grate)\b", re.IGNORECASE)
_SEWER = re.compile(r"\b(sewage|sewer|back ?up|backed up|toilet|odou?r|smell\w*|manhole)\b", re.IGNORECASE)
_LEAK = re.compile(r"\b(leak\w*|water main|hydrant|broken pipe)\b", re.IGNORECASE)
_TREE_DOWN = re.compile(r"\b(fallen|fell|down(ed)?|uproot\w*|blocking|broken|snapped|limb|branch\w*|leaning)\b", re.IGNORECASE)


def utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def pacific(ts: str | None) -> datetime | None:
    """DataSF publishes floating Pacific-time timestamps."""
    if not ts:
        return None
    d = datetime.fromisoformat(ts)
    return (d if d.tzinfo else d.replace(tzinfo=PT)).astimezone(UTC)


def classify_text(text: str | None) -> str:
    t = text or ""
    for rx, cat in ((_FLOOD, "flooding"), (_SEWER, "sewer_backup"), (_LEAK, "water_leak"), (_DRAIN, "catch_basin")):
        if rx.search(t):
            return cat
    return "other"


def report_doc(case_id, source, category, requested_at, lat, lon, **fields) -> dict | None:
    if not in_sf(lat, lon):
        return None
    doc = {"_id": f"311:{case_id}", "case_id": case_id, "category": category, "requested_at": requested_at.isoformat(),
           "location": {"lat": lat, "lon": lon}, "h3_9": cell(lat, lon), f"seen_{source}": True}  # fmt: skip
    doc.update({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fields.items() if v is not None})
    return doc


RANK = {"keywords": 0, "service_code": 1, "mistral": 2, "datasf": 3}


def merge(old: dict | None, new: dict) -> dict:
    """Two views of one 311 case: the stronger category source wins; the newest status wins."""
    derived = {"zone_100yr", "slr24", "zones"}
    if not old:
        return {k: v for k, v in new.items() if k not in derived}
    out = {k: v for k, v in old.items() if k not in derived}
    better = RANK.get(new.get("category_source"), 0) >= RANK.get(old.get("category_source"), 0)
    newer = (new.get("updated_at") or "") >= (old.get("updated_at") or "")
    for k, v in new.items():
        if k in derived or k == "_id":
            continue
        if k in ("category", "subcategory", "category_source") and not better:
            continue
        if k in ("status", "updated_at") and not newer:
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------- Open311 (live)

OPEN311 = "https://mobile311.sfgov.org/open311/v2/requests.json"
OPEN311_CODES = {"PUC:Water:Sewer": None, "PW:BUF:Tree Maintenance": "tree", "input:Pothole & Street Issues": "street"}


def open311_doc(r: dict) -> dict | None:
    if r.get("lat") is None or not r.get("requested_datetime"):
        return None
    code = r.get("service_code") or ""
    fixed = OPEN311_CODES.get(code)
    desc = r.get("description") or ""
    cat = fixed or classify_text(desc)
    sub = ("fallen_or_damaged" if _TREE_DOWN.search(desc) else "maintenance") if cat == "tree" else None
    return report_doc(str(r["service_request_id"]), "open311", cat, utc(r["requested_datetime"]), float(r["lat"]), float(r["long"]),
                      subcategory=sub, service_code=code, status=r.get("status"), updated_at=utc(r.get("updated_datetime")),
                      address=r.get("address"), description=desc[:2000] or None, media_url=r.get("media_url") or None,
                      category_source="service_code" if fixed else "keywords")  # fmt: skip


def fetch_open311(http: httpx.Client, since: datetime) -> list[dict]:
    out = []
    for code in OPEN311_CODES:
        for page in range(1, 11):
            rows = http.get(OPEN311, params={"service_code": code, "start_date": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "page_size": 200, "page": page}).json()
            out += [d for d in map(open311_doc, rows) if d]
            if len(rows) < 200:
                break
    return out


# ---------------------------------------------------------------- DataSF 311 (history)

DATASF = "https://data.sf.gov/resource/vw6y-z8j6.json"
SEWER_SUB = {"flooding": "flooding", "sewage_back_up": "sewer_backup", "sewage_back-up_discharge": "sewer_backup", "side_sewer_issues": "sewer_backup",
             "water_leak": "water_leak", "water_main_break": "water_leak", "catch_basin": "catch_basin", "catch_basin_other": "catch_basin"}  # fmt: skip
SERVICES = ("Sewer Issues", "Sewer", "Catch Basin Maintenance", "Tree Maintenance", "Street Defects")


def datasf_doc(r: dict) -> dict | None:
    if not r.get("lat") or not r.get("requested_datetime"):
        return None
    name, st = r.get("service_name", ""), (r.get("service_subtype") or "").lower()
    if name in ("Sewer Issues", "Sewer"):
        cat, sub = SEWER_SUB.get(st, "other"), st
    elif name == "Catch Basin Maintenance":
        cat, sub = "catch_basin", st
    elif name == "Tree Maintenance":
        if not any(k in st for k in ("damaged", "damaging", "tree_other")):
            return None
        cat, sub = "tree", "fallen_or_damaged"
    elif name == "Street Defects":
        cat, sub = "street", st
    else:
        return None
    return report_doc(str(r["service_request_id"]), "datasf", cat, pacific(r["requested_datetime"]), float(r["lat"]), float(r["long"]),
                      subcategory=sub, status=(r.get("status_description") or "").lower() or None, updated_at=pacific(r.get("updated_datetime")),
                      closed_at=pacific(r.get("closed_date")), address=r.get("address"), neighborhood=r.get("analysis_neighborhood"),
                      channel=r.get("source"), category_source="datasf")  # fmt: skip


def fetch_datasf(http: httpx.Client, start: datetime, end: datetime | None = None, field: str = "requested_datetime", services=SERVICES):
    names = ",".join(f"'{n}'" for n in services)
    where = f"service_name in ({names}) AND {field} >= '{start.astimezone(PT):%Y-%m-%dT%H:%M:%S}'"
    if end:
        where += f" AND {field} < '{end.astimezone(PT):%Y-%m-%dT%H:%M:%S}'"
    offset = 0
    while True:
        rows = http.get(DATASF, params={"$where": where, "$order": ":id", "$limit": 50000, "$offset": offset}, timeout=180).json()
        yield from (d for d in map(datasf_doc, rows) if d)
        if len(rows) < 50000:
            return
        offset += 50000


# ---------------------------------------------------------------- NWS, NOAA, Open-Meteo, CHP

RELEVANT = {"Flood Watch", "Flood Warning", "Flood Advisory", "Flash Flood Watch", "Flash Flood Warning", "Coastal Flood Advisory",
            "Coastal Flood Watch", "Coastal Flood Warning", "Coastal Flood Statement", "Wind Advisory", "High Wind Warning", "Beach Hazards Statement"}  # fmt: skip
VTEC = {("FA", "A"): "Flood Watch", ("FA", "W"): "Flood Warning", ("FA", "Y"): "Flood Advisory", ("FF", "W"): "Flash Flood Warning",
        ("CF", "Y"): "Coastal Flood Advisory", ("CF", "W"): "Coastal Flood Warning", ("WI", "Y"): "Wind Advisory", ("HW", "W"): "High Wind Warning"}  # fmt: skip


def nws_docs(payload: dict) -> list[dict]:
    out = []
    for f in payload.get("features", []):
        p = f["properties"]
        if p.get("event") not in RELEVANT:
            continue
        s, e = utc(p.get("onset") or p.get("effective")), utc(p.get("ends") or p.get("expires"))
        out.append({"_id": f"nws:{p['id']}", "@timestamp": (utc(p.get("sent")) or s).isoformat(), "kind": "nws_alert", "event": p["event"],
                    "headline": p.get("headline"), "valid_from": s and s.isoformat(), "valid_to": e and e.isoformat(), "url": p.get("@id"), "source": "nws"})  # fmt: skip
    return out


def iem_docs(payload: dict, start: str, end: str, replay: str) -> list[dict]:
    """Archived NWS products (IEM VTEC) overlapping [start, end) for SF zones."""
    seen = {}
    for e in payload.get("events", []):
        ev = VTEC.get((e.get("phenomena"), e.get("significance")))
        if not ev or e.get("ugc") not in SF_UGC or not (e["issue"] < end and e["expire"] > start):
            continue
        key = f"{e['phenomena']}.{e['significance']}.{e['eventid']}"
        seen.setdefault(key, {"_id": f"iem:{replay}:{key}", "@timestamp": e.get("product_issued") or e["issue"], "kind": "nws_alert", "event": ev,
                              "valid_from": e["issue"], "valid_to": e["expire"], "issued_at": e.get("product_issued") or e["issue"],
                              "url": e.get("url"), "source": "iem", "replay": replay})  # fmt: skip
    return list(seen.values())


def tide_docs(payload: dict, kind: str) -> list[dict]:
    rows = payload.get("predictions") or payload.get("data") or []
    out = []
    for p in rows:
        if p.get("v") in (None, ""):
            continue
        t = datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        out.append({"_id": f"{kind}:{t:%Y%m%d%H%M}", "@timestamp": t.isoformat(), "kind": kind, "value": float(p["v"]), "tide_type": p.get("type"), "source": "noaa"})
    return out


def weather_docs(payload, now: datetime | None, tag: str = "wx", source: str = "open-meteo", replay: str | None = None) -> list[dict]:
    blocks = payload if isinstance(payload, list) else [payload]
    out = []
    for name, b in zip(POINTS, blocks, strict=False):
        h = b["hourly"]
        for t, r, g in zip(h["time"], h["precipitation"], h.get("wind_gusts_10m") or [None] * len(h["time"]), strict=False):
            ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
            d = {"_id": f"{tag}:{name}:{ts:%Y%m%d%H}", "@timestamp": ts.isoformat(), "kind": "weather", "point": name, "rain_mm": r or 0.0,
                 "gust_kmh": g, "forecast": bool(now and ts > now), "source": source}  # fmt: skip
            if replay:
                d["replay"] = replay
            out.append(d)
    return out


def chp_docs(xml_text: str, now: datetime) -> list[dict]:
    out = []
    for log in ET.fromstring(xml_text).iter("Log"):
        typ = (log.findtext("LogType") or "").strip('"')
        if not any(k in typ for k in ("Traffic Hazard", "CLOSURE", "Flood", "Tree", "Debris")):
            continue
        ll = (log.findtext("LATLON") or "").strip('"')
        if ":" not in ll:
            continue
        lat, lon = int(ll.split(":")[0]) / 1e6, -int(ll.split(":")[1]) / 1e6
        if not in_sf(lat, lon):
            continue
        details = " ".join((d.findtext("IncidentDetail") or "").strip('"') for d in log.iter("details"))
        when = datetime.strptime(" ".join((log.findtext("LogTime") or "").strip('"').split()), "%b %d %Y %I:%M%p").replace(tzinfo=PT)
        out.append({"_id": f"chp:{log.get('ID')}", "type": typ, "location": {"lat": lat, "lon": lon}, "h3_9": cell(lat, lon),
                    "place": (log.findtext("Location") or "").strip('"'), "flooding": bool(re.search(r"flood|water", details + typ, re.IGNORECASE)),
                    "started_at": when.astimezone(UTC).isoformat(), "last_seen": now.isoformat()})  # fmt: skip
    return out


def _pts() -> dict:
    return {"latitude": ",".join(str(p[0]) for p in POINTS.values()), "longitude": ",".join(str(p[1]) for p in POINTS.values()),
            "hourly": "precipitation,wind_gusts_10m", "timezone": "UTC"}  # fmt: skip


FEEDS = {  # name: (interval seconds, index, fetcher(http, since, now) -> docs)
    "open311": (120, "reports", lambda h, since, now: fetch_open311(h, since or now - timedelta(days=2))),
    "nws": (300, "conditions", lambda h, since, now: nws_docs(h.get("https://api.weather.gov/alerts/active", params={"zone": ",".join(SF_UGC)}).json())),
    "weather": (900, "conditions", lambda h, since, now: weather_docs(h.get("https://api.open-meteo.com/v1/forecast", params={**_pts(), "past_days": 1, "forecast_days": 2}).json(), now)),
    "tides": (1800, "conditions", lambda h, since, now: tide_docs(h.get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", params={
        "station": "9414290", "product": "predictions", "interval": "hilo", "datum": "MLLW", "units": "english", "time_zone": "gmt", "format": "json",
        "begin_date": f"{now:%Y%m%d}", "range": 48}).json(), "tide_pred")),  # fmt: skip
    "chp": (300, "incidents", lambda h, since, now: chp_docs(h.get("https://media.chp.ca.gov/sa_xml/sa.xml").text, now)),
}


def client() -> httpx.Client:
    return httpx.Client(timeout=30, headers=UA, follow_redirects=True)
