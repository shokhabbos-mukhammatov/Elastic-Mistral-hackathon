"""Elasticsearch: mappings, Mistral inference endpoints, geo_match enrich pipeline, and idempotent writes."""

import logging
from datetime import UTC, datetime

from elasticsearch import ApiError, Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk

from highground.config import settings

log = logging.getLogger("highground.store")
KW, DATE, GEO, BOOL, FLOAT, TEXT = ({"type": t} for t in ("keyword", "date", "geo_point", "boolean", "float", "text"))

MAPPINGS = {
    "reports": {"case_id": KW, "category": KW, "subcategory": KW, "category_source": KW, "service_code": KW, "status": KW,
                "requested_at": DATE, "updated_at": DATE, "closed_at": DATE, "location": GEO, "h3_9": KW, "address": TEXT,
                "neighborhood": KW, "description": TEXT, "media_url": {"type": "keyword", "index": False}, "zone_100yr": BOOL, "slr24": BOOL,
                "vision": {"properties": {"water_visible": BOOL, "confidence": FLOAT}}},
    "zones": {"layer": KW, "name": KW, "geometry": {"type": "geo_shape"}},
    "conditions": {"@timestamp": DATE, "kind": KW, "point": KW, "rain_mm": FLOAT, "gust_kmh": FLOAT, "forecast": BOOL, "value": FLOAT,
                   "tide_type": KW, "event": KW, "headline": TEXT, "valid_from": DATE, "valid_to": DATE, "issued_at": DATE, "source": KW, "replay": KW},
    "incidents": {"type": KW, "location": GEO, "h3_9": KW, "place": TEXT, "flooding": BOOL, "started_at": DATE},
    "places": {"kind": KW, "name": {"type": "search_as_you_type"}, "alt": {"type": "search_as_you_type"}, "location": GEO, "neighborhood": KW},
    "profiles": {"h3_9": KW, "in_100yr": BOOL, "slr24": BOOL, "reports_5y": {"type": "integer"}, "storm_reports_5y": {"type": "integer"},
                 "storm_days_5y": {"type": "integer"}, "neighborhood": KW, "centroid": GEO},
    "risk": {"h3_9": KW, "level": KW, "rank": {"type": "byte"}, "score": FLOAT, "reasons": {"type": "object", "enabled": False},
             "as_of": DATE, "polygon": {"type": "geo_shape"}},
    "guides": {"url": KW, "lang": KW, "title": TEXT, "body": TEXT, "content": TEXT},
    "feeds": {"last_ok": DATE, "watermark": DATE, "error": TEXT},
}  # fmt: skip
SETTINGS = {"profiles": {"index.mode": "lookup"}}  # joinable from ES|QL LOOKUP JOIN
PIPELINE, POLICY = "reports-enrich", "zones-policy"
EMBED, CHAT, COMPLETION = "mistral-embeddings", "mistral-chat", "mistral-completion"


class Store:
    def __init__(self, client: Elasticsearch | None = None):
        cfg = settings()
        self.cfg = cfg
        self.es = client or (Elasticsearch(cfg.es_url, api_key=cfg.es_api_key, request_timeout=60) if cfg.es_api_key else Elasticsearch(cfg.es_url, request_timeout=60))

    def ix(self, name: str) -> str:
        return self.cfg.ix(name)

    def write(self, name: str, docs: list[dict], pipeline: bool = False, merge_fn=None, refresh=False) -> int:
        if not docs:
            return 0
        if merge_fn:
            ids = list({d["_id"] for d in docs})
            existing = {}
            for i in range(0, len(ids), 1000):
                try:
                    for d in self.es.mget(index=self.ix(name), ids=ids[i : i + 1000])["docs"]:
                        if d.get("found"):
                            existing[d["_id"]] = d["_source"]
                except NotFoundError:
                    pass
            merged = {}
            for d in docs:
                merged[d["_id"]] = merge_fn(merged.get(d["_id"]) or existing.get(d["_id"]), d)
            docs = [{**v, "_id": k} for k, v in merged.items()]
        actions = [{"_index": self.ix(name), "_id": d["_id"], "_source": {k: v for k, v in d.items() if k != "_id"}} for d in docs]
        use = self.ix(PIPELINE) if pipeline and self.has_pipeline() else None
        ok, errors = bulk(self.es.options(request_timeout=180), actions, pipeline=use, refresh=refresh, chunk_size=1000, raise_on_error=False)
        if errors:  # keep going: one malformed polygon or row must not drop the rest of the batch
            log.warning("%d/%d rejected in %s: %s", len(errors), len(actions), name, str(errors[0])[:200])
        return ok

    def has_pipeline(self) -> bool:
        try:
            self.es.ingest.get_pipeline(id=self.ix(PIPELINE))
            return True
        except NotFoundError:
            return False

    def search(self, name: str, **body) -> dict:
        return self.es.search(index=self.ix(name), **body).body

    def esql(self, query: str, params: list | None = None) -> list[dict]:
        r = self.es.esql.query(query=query, params=params or []).body
        cols = [c["name"] for c in r["columns"]]
        return [dict(zip(cols, row, strict=False)) for row in r["values"]]

    def feed_state(self, name: str) -> dict:
        try:
            return self.es.get(index=self.ix("feeds"), id=name)["_source"]
        except NotFoundError:
            return {}

    def save_feed(self, name: str, state: dict) -> None:
        self.es.index(index=self.ix("feeds"), id=name, document=state)


def setup(s: Store) -> dict:
    out = {"inference": {}}
    if s.cfg.has_mistral:  # Mistral models as Elasticsearch inference endpoints (hack night guide names)
        for task, eid, model in (("text_embedding", EMBED, s.cfg.model_embed), ("chat_completion", CHAT, s.cfg.model_large),
                                 ("completion", COMPLETION, s.cfg.model_small)):  # fmt: skip
            try:
                s.es.inference.get(task_type=task, inference_id=eid)
            except NotFoundError:
                try:
                    s.es.inference.put(task_type=task, inference_id=eid, inference_config={"service": "mistral", "service_settings": {"api_key": s.cfg.mistral_api_key, "model": model}})
                except ApiError as exc:
                    log.warning("endpoint %s: %s", eid, exc)
                    continue
            out["inference"][eid] = True
    created = []
    for name, props in MAPPINGS.items():
        idx = s.ix(name)
        if s.es.indices.exists(index=idx):
            continue
        props = dict(props)
        if name == "guides" and out["inference"].get(EMBED):
            props["content"] = {"type": "semantic_text", "inference_id": EMBED}
        s.es.indices.create(index=idx, mappings={"properties": props}, settings=SETTINGS.get(name))
        created.append(idx)
    out["created"] = created
    return out


def enrich_pipeline(s: Store) -> None:
    """geo_match enrich: tag every 311 report with flood zone / sea-level-rise zone / neighborhood at ingest."""
    for fn, kw in ((s.es.ingest.delete_pipeline, {"id": s.ix(PIPELINE)}), (s.es.enrich.delete_policy, {"name": s.ix(POLICY)})):
        try:
            fn(**kw)
        except NotFoundError:
            pass
    s.es.enrich.put_policy(name=s.ix(POLICY), geo_match={"indices": s.ix("zones"), "match_field": "geometry", "enrich_fields": ["layer", "name"]})
    s.es.enrich.execute_policy(name=s.ix(POLICY), wait_for_completion=True)
    s.es.ingest.put_pipeline(id=s.ix(PIPELINE), processors=[
        {"enrich": {"policy_name": s.ix(POLICY), "field": "location", "target_field": "z", "shape_relation": "INTERSECTS", "max_matches": 16, "ignore_missing": True, "ignore_failure": True}},
        {"script": {"ignore_failure": True, "source": """
            ctx.zone_100yr = false; ctx.slr24 = false;
            if (ctx.z != null) { for (def m : (ctx.z instanceof List ? ctx.z : [ctx.z])) {
              if (m.layer == '100yr') ctx.zone_100yr = true; if (m.layer == 'slr24') ctx.slr24 = true;
              if (m.layer == 'neighborhood' && ctx.neighborhood == null) ctx.neighborhood = m.name; } ctx.remove('z'); }"""}},
        {"set": {"field": "ingested_at", "value": "{{{_ingest.timestamp}}}"}},
    ])  # fmt: skip


def run_feed(s: Store, name: str, http, now: datetime | None = None) -> dict:
    from highground.feeds import FEEDS, merge

    now = now or datetime.now(UTC)
    _, index, fetch = FEEDS[name]
    st = s.feed_state(name)
    since = datetime.fromisoformat(st["watermark"]) if st.get("watermark") else None
    try:
        docs = fetch(http, since, now)
        n = s.write(index, docs, pipeline=index == "reports", merge_fn=merge if index == "reports" else None)
        s.save_feed(name, {"last_ok": now.isoformat(), "watermark": now.isoformat() if name == "open311" else None, "rows": n, "error": None, "fails": 0})
        return {"feed": name, "rows": n}
    except Exception as exc:  # noqa: BLE001 - recorded, never crashes the worker
        s.save_feed(name, {**st, "error": f"{type(exc).__name__}: {exc}"[:300], "fails": st.get("fails", 0) + 1, "last_try": now.isoformat()})
        return {"feed": name, "error": str(exc)[:200]}
