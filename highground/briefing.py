"""Block briefing: Elasticsearch facts -> deterministic risk -> Mistral explanation (checked against the facts)."""

import json
import re
from datetime import UTC, datetime, timedelta

from highground import geo
from highground.risk import ACTIONS, HELP, Inputs, Risk, assess

LANGS = {"en": "English", "es": "Spanish", "zh": "Chinese (Traditional)", "tl": "Filipino", "vi": "Vietnamese", "ru": "Russian", "fr": "French"}


def live_inputs(s, lat: float, lon: float, now: datetime) -> tuple[Inputs, dict]:
    """One msearch: zones at the exact point, nearby reports, CHP incidents, weather, NWS, tide, block profile."""
    c = geo.cell(lat, lon)
    ring = geo.disk(c, 1)
    pt = geo.nearest_point(lat, lon)
    iso = now.isoformat()
    q = [
        ("zones", {"size": 0, "query": {"geo_shape": {"geometry": {"shape": {"type": "point", "coordinates": [lon, lat]}, "relation": "intersects"}}},
                   "aggs": {"l": {"terms": {"field": "layer"}}, "n": {"terms": {"field": "name", "size": 1, "exclude": ["100yr", "slr24"]}}}}),
        ("reports", {"size": 50, "sort": [{"requested_at": "desc"}], "_source": ["category", "requested_at", "address", "status", "location", "h3_9"],
                     "query": {"bool": {"filter": [{"terms": {"h3_9": ring}}, {"range": {"requested_at": {"gte": (now - timedelta(hours=6)).isoformat(), "lte": iso}}}]}}}),
        ("incidents", {"size": 20, "query": {"bool": {"filter": [{"geo_distance": {"distance": "800m", "location": {"lat": lat, "lon": lon}}},
                                                                 {"range": {"started_at": {"gte": (now - timedelta(hours=6)).isoformat()}}}]}}}),
        ("conditions", {"size": 60, "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": [{"term": {"kind": "weather"}}, {"term": {"point": pt}},
                         {"range": {"@timestamp": {"gte": (now - timedelta(hours=4)).isoformat(), "lte": (now + timedelta(hours=3)).isoformat()}}}],
                         "must_not": [{"exists": {"field": "replay"}}]}}}),
        ("conditions", {"size": 20, "query": {"bool": {"filter": [{"term": {"kind": "nws_alert"}}, {"range": {"valid_to": {"gt": iso}}}],
                                                       "must_not": [{"exists": {"field": "replay"}}]}}}),
        ("conditions", {"size": 1, "sort": [{"value": "desc"}], "query": {"bool": {"filter": [{"terms": {"kind": ["tide_obs", "tide_pred"]}},
                         {"range": {"@timestamp": {"gte": (now - timedelta(hours=1)).isoformat(), "lte": (now + timedelta(hours=3)).isoformat()}}}]}}}),
        ("profiles", {"size": 1, "query": {"term": {"h3_9": c}}}),
        ("reports", {"size": 0, "query": {"bool": {"filter": [{"terms": {"h3_9": ring}}, {"terms": {"category": ["flooding", "catch_basin"]}},
                                                             {"range": {"requested_at": {"gte": (now - timedelta(days=5 * 365)).isoformat()}}}]}},
                     "aggs": {"years": {"date_histogram": {"field": "requested_at", "calendar_interval": "year"}},
                              "top_days": {"terms": {"script": {"source": "doc['requested_at'].value.withZoneSameInstant(ZoneId.of('America/Los_Angeles')).toLocalDate().toString()"}, "size": 3}}}}),
    ]  # fmt: skip
    body = []
    for idx, b in q:
        body += [{"index": s.ix(idx), "ignore_unavailable": True}, b]
    rs = s.es.msearch(searches=body)["responses"]
    zl = {b["key"] for b in rs[0].get("aggregations", {}).get("l", {}).get("buckets", [])}
    nb = rs[0].get("aggregations", {}).get("n", {}).get("buckets", [])
    reports = [h["_source"] for h in rs[1].get("hits", {}).get("hits", [])]
    incidents = [h["_source"] for h in rs[2].get("hits", {}).get("hits", [])]
    wx = [h["_source"] for h in rs[3].get("hits", {}).get("hits", [])]
    alerts = [h["_source"] for h in rs[4].get("hits", {}).get("hits", [])]
    tide = [h["_source"] for h in rs[5].get("hits", {}).get("hits", [])]
    prof = (rs[6].get("hits", {}).get("hits") or [{"_source": {}}])[0]["_source"]
    hist = rs[7].get("aggregations", {})

    def ts(x):
        return datetime.fromisoformat(x["@timestamp"])

    obs = [w for w in wx if ts(w) + timedelta(hours=1) <= now]
    fc = [w for w in wx if ts(w) >= now - timedelta(hours=1) and w not in obs]
    i = Inputs(in_100yr="100yr" in zl, slr24="slr24" in zl, storm_reports_5y=prof.get("storm_reports_5y", 0),
               rain_1h=sum(w["rain_mm"] for w in obs if ts(w) >= now - timedelta(hours=2)), rain_3h=sum(w["rain_mm"] for w in obs if ts(w) >= now - timedelta(hours=4)),
               rain_next3h=sum(w["rain_mm"] for w in fc[:3]), gust_kmh=max((w.get("gust_kmh") or 0 for w in wx if ts(w) <= now + timedelta(hours=3)), default=0),
               nws=sorted({a["event"] for a in alerts if a.get("valid_from", "") <= (now + timedelta(hours=12)).isoformat()}), tide_ft=tide[0]["value"] if tide else None,
               reports=[(r["category"], (now - datetime.fromisoformat(r["requested_at"])).total_seconds() / 3600) for r in reports],
               incidents_flooding=sum(1 for x in incidents if x.get("flooding")), rain_point=pt,
               sources={"zone_100yr": "SFPUC 100-Year Storm Flood Risk Map (DataSF jzu3-4yxp)", "history": "DataSF 311 (vw6y-z8j6)", "rain_3h": "Open-Meteo",
                        "rain_next3h": "Open-Meteo forecast", "nws": "api.weather.gov", "reports": "SF311 Open311 + DataSF", "chp": "CHP incident feed",
                        "tide": "NOAA CO-OPS 9414290", "wind": "Open-Meteo"})  # fmt: skip
    ctx = {"cell": c, "neighborhood": nb[0]["key"] if nb else None, "zones": sorted(zl), "reports": reports[:10], "incidents": incidents[:5],
           "alerts": list({(a["event"], a.get("valid_to")): {"event": a["event"], "until": a.get("valid_to"), "headline": a.get("headline")} for a in alerts}.values()),
           "tide_ft": i.tide_ft, "history": {"flood_reports_5y": sum(b["doc_count"] for b in hist.get("years", {}).get("buckets", [])),
                                             "top_days": [{"day": b["key"], "reports": b["doc_count"]} for b in hist.get("top_days", {}).get("buckets", [])]}}  # fmt: skip
    return i, ctx


def template(risk: Risk, ctx: dict, place: str) -> str:
    head = {"low": "Low risk", "elevated": "Elevated risk", "high": "High risk", "severe": "Severe risk"}[risk.level]
    why = "; ".join(r["text"] for r in risk.reasons[:4]) or "no active rain, alerts or reports"
    return f"{head} of flooding or storm hazards at {place} right now. Why: {why}. {ACTIONS[risk.level][0]}"


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def receipts(text: str, facts: dict) -> tuple[str, list[str]]:
    """Drop any sentence containing a number that does not appear in the facts we gave the model."""
    allowed = {n.replace(",", "") for n in _NUM.findall(json.dumps(facts, ensure_ascii=False, default=str))}
    allowed |= {str(int(float(n))) for n in allowed if n.replace(".", "", 1).isdigit()}
    keep, dropped = [], []
    for sent in re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*", text.strip()):  # never split inside "9.4"
        nums = [n.replace(",", "") for n in _NUM.findall(sent)]
        (keep if all(n in allowed or n in ("1", "311", "911") for n in nums) else dropped).append(sent)
    return " ".join(k.strip() for k in keep if k.strip()), dropped


def narrate(cfg, risk: Risk, ctx: dict, place: str, lang: str) -> dict:
    """Mistral explains the deterministic result in the resident's language. Falls back to the template."""
    facts = {"place": place, "level": risk.level, "reasons": [{k: r[k] for k in ("text", "value", "unit")} for r in risk.reasons],
             "actions": ACTIONS[risk.level], "alerts": ctx["alerts"], "history": ctx["history"], "recent_reports": len(ctx["reports"])}  # fmt: skip
    fallback = {"text": template(risk, ctx, place), "lang": "en", "by": "template", "dropped": []}
    if not cfg.has_mistral:
        return fallback
    try:
        from highground import llm

        m = llm.client(timeout_ms=25000)
        r = m.chat.complete(model=cfg.model_large, temperature=0.2, max_tokens=1500, **llm.extra(cfg.model_large), messages=[
            {"role": "system", "content": "You are HighGround SF, a flood and storm-safety helper for San Francisco residents. Write 3-5 short sentences at a 6th-grade "
             "reading level in " + LANGS.get(lang, "the user's language") + ". Use ONLY the facts in the JSON. Never invent numbers, places, times or programs. "
             "Say the risk level, the main reasons, and the 2 most important actions. Do not mention JSON. Not an official alert; for emergencies call 911."},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}])  # fmt: skip
        text, dropped = receipts(llm.text(r.choices[0].message.content), facts)
        if len(dropped) > 2 or not text:
            return {**fallback, "dropped": dropped}
        return {"text": text, "lang": lang, "by": cfg.model_large, "dropped": dropped}
    except Exception as exc:  # noqa: BLE001 - the briefing must work without Mistral
        return {**fallback, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def briefing(s, lat: float, lon: float, place: str, lang: str = "en", now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    i, ctx = live_inputs(s, lat, lon, now)
    risk = assess(i, now)
    return {"place": place, "location": {"lat": lat, "lon": lon}, "cell": ctx["cell"], "neighborhood": ctx["neighborhood"],
            "risk": {"level": risk.level, "score": risk.score, "components": {"susceptibility": risk.s, "hazard": risk.h, "confirmation": risk.c}},
            "reasons": risk.reasons, "actions": ACTIONS[risk.level], "help": HELP, "zones": ctx["zones"], "nearby": {"reports": ctx["reports"], "incidents": ctx["incidents"]},
            "alerts": ctx["alerts"], "history": ctx["history"], "inputs": {"rain_1h": round(i.rain_1h, 1), "rain_3h": round(i.rain_3h, 1), "rain_next3h": round(i.rain_next3h, 1),
            "gust_kmh": i.gust_kmh, "tide_ft": i.tide_ft}, "narrative": narrate(s.cfg, risk, ctx, place, lang), "as_of": now.isoformat(),
            "disclaimer": "Not an official alert. Call 911 in an emergency. Official alerts: AlertSF."}  # fmt: skip
