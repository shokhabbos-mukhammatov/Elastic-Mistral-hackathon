"""HighGround SF web app: briefing, ask, report, map tiles, replay/backtest, status. Reads only Elasticsearch + Mistral."""

import logging
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from highground import briefing as br
from highground import geo, layers, replay, store
from highground.feeds import FEEDS

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="HighGround SF", version="hacknight")
WEB = Path(__file__).parent / "web"


@lru_cache
def S() -> store.Store:
    return store.Store()


def locate(address: str | None, lat: float | None, lon: float | None) -> dict:
    if lat is not None and lon is not None:
        if not geo.in_sf(lat, lon):
            raise HTTPException(422, "That point is outside San Francisco.")
        return {"lat": lat, "lon": lon, "label": f"{lat:.5f}, {lon:.5f}"}
    if not address:
        raise HTTPException(422, "Give an address, an intersection, or lat/lon.")
    g = layers.geocode(S(), address)
    if not g:
        raise HTTPException(404, f"Could not find '{address}' in San Francisco. Try an intersection like '17th and Folsom'.")
    return g


@app.get("/api/briefing")
def get_briefing(address: str | None = None, lat: float | None = None, lon: float | None = None, lang: str = "en"):
    g = locate(address, lat, lon)
    return br.briefing(S(), g["lat"], g["lon"], g["label"], lang)


@app.get("/api/geocode")
def get_geocode(q: str):
    return locate(q, None, None)


class Ask(BaseModel):
    question: str
    lang: str | None = None
    lat: float | None = None
    lon: float | None = None


@app.post("/api/ask")
def post_ask(a: Ask):
    from highground import agent

    return agent.ask(S(), a.question, a.lat, a.lon)


@app.post("/api/report")
async def post_report(lat: float = Form(...), lon: float = Form(...), text: str = Form(""), photo: UploadFile | None = File(None), audio: UploadFile | None = File(None)):
    from highground import report

    img = await photo.read() if photo else None
    snd = await audio.read() if audio else None
    if (img and len(img) > 8_000_000) or (snd and len(snd) > 10_000_000):
        raise HTTPException(413, "File too large")
    return report.draft(S(), lat, lon, text, img, photo.content_type if photo else None, snd)


@app.get("/api/nearby")
def nearby(lat: float, lon: float, hours: int = Query(24, le=168), radius_m: int = Query(500, le=3000)):
    """Text twin of the map: every recent report near a point, closest first."""
    r = S().search("reports", size=100, sort=[{"_geo_distance": {"location": {"lat": lat, "lon": lon}, "order": "asc", "unit": "m"}}],
                   query={"bool": {"filter": [{"geo_distance": {"distance": f"{radius_m}m", "location": {"lat": lat, "lon": lon}}},
                                              {"range": {"requested_at": {"gte": f"now-{hours}h"}}}]}},
                   _source=["category", "requested_at", "address", "status", "location"])  # fmt: skip
    return [{**h["_source"], "distance_m": round(h["sort"][0])} for h in r["hits"]["hits"]]


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf")
def tiles(layer: str, z: int, x: int, y: int, hours: int = 72):
    """Vector tiles straight from Elasticsearch _mvt (points + H3 geohex aggregation)."""
    if layer == "reports":
        body = {"grid_agg": "geohex", "grid_precision": 4, "size": 2000, "fields": ["category", "requested_at", "status"],
                "query": {"range": {"requested_at": {"gte": f"now-{hours}h"}}}}  # fmt: skip
        idx, field = "reports", "location"
    elif layer == "history":
        body = {"grid_agg": "geohex", "grid_precision": 6, "size": 0, "query": {"terms": {"category": ["flooding", "catch_basin"]}}}
        idx, field = "reports", "location"
    elif layer == "zones":
        body = {"size": 5000, "grid_precision": 0, "fields": ["layer"], "query": {"term": {"layer": "100yr"}}}
        idx, field = "zones", "geometry"
    else:
        raise HTTPException(404)
    try:
        data = S().es.search_mvt(index=S().ix(idx), field=field, zoom=z, x=x, y=y, **body)
    except Exception as exc:
        raise HTTPException(502, str(exc)[:200]) from exc
    return Response(bytes(data.body), media_type="application/vnd.mapbox-vector-tile", headers={"Cache-Control": "max-age=60"})


@lru_cache(maxsize=4)
def _replay(day: str) -> replay.Replay:
    return replay.Replay(replay.load(day))


@app.get("/api/replay/{day}")
def get_replay(day: str, t: str | None = None):
    try:
        rp = _replay(day)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"No replay dataset for {day}. Run: python -m highground.ingest replay {day}") from exc
    start, end = replay.day_bounds(day)
    if not t:
        hours = []
        for h in range(24):
            ts = start + timedelta(hours=h)
            rain = sum(w[1] for w in rp.rain.get("c", []) if ts <= w[0] < ts + timedelta(hours=1))
            n = sum(1 for x, r in rp.reports if ts <= x < ts + timedelta(hours=1) and r["category"] in replay.FLOOD)
            hours.append({"t": ts.isoformat(), "rain_mm": round(rain, 1), "flood_reports": n})
        return {"day": day, "start": start.isoformat(), "end": end.isoformat(), "hours": hours, "weather_source": rp.ds["weather_source"],
                "nws": rp.ds["nws"], "cells": len(rp.cells())}  # fmt: skip
    ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
    f = rp.frame(ts if ts.tzinfo else ts.replace(tzinfo=UTC))
    f["polygons"] = {c: geo.polygon(c) for c in f["cells"]}
    return f


@lru_cache(maxsize=8)
def _backtest(day: str, threshold: str) -> dict:
    return replay.backtest(replay.load(day), threshold=threshold)


@app.get("/api/backtest/{day}")
def get_backtest(day: str, threshold: str = "high"):
    return _backtest(day, threshold)


@app.get("/api/ops")
def get_ops():
    from highground import ops

    return ops.overview(S())


@app.get("/status")
def status():
    out = {}
    for name, (interval, _, _) in FEEDS.items():
        st = S().feed_state(name)
        age = (datetime.now(UTC) - datetime.fromisoformat(st["last_ok"])).total_seconds() if st.get("last_ok") else None
        out[name] = {"age_s": round(age) if age is not None else None, "fresh": age is not None and age < 3 * interval, "error": st.get("error")}
    return {"feeds": out, "mistral": S().cfg.has_mistral}


@app.get("/healthz")
def healthz():
    try:
        S().es.info()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": type(exc).__name__}, 503)


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
