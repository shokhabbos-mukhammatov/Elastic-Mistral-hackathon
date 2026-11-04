"""City-ops view: ES|QL (STATS, LOOKUP JOIN on the lookup-mode profiles index, COMPLETION with Mistral) + significant_terms."""

from datetime import UTC, datetime, timedelta

from highground.store import COMPLETION

CATS = '("flooding", "catch_basin", "sewer_backup", "tree")'


def citywide(s, hours: int = 24) -> dict:
    now = datetime.now(UTC)
    rows = s.esql(f"""FROM {s.ix("reports")}
| WHERE requested_at >= NOW() - TO_TIMEDURATION(CONCAT(TO_STRING(?hours), " hours")) AND category IN {CATS}
| STATS reports = COUNT(*), flooding = COUNT(CASE(category == "flooding", 1, null)) BY neighborhood
| SORT reports DESC | LIMIT 10""", [{"hours": hours}])  # fmt: skip
    # "unusual right now": which neighborhoods are over-represented vs the last 5 years (explicit timestamps: cacheable)
    sig = s.search("reports", size=0, query={"bool": {"filter": [{"range": {"requested_at": {"gte": (now - timedelta(hours=hours)).isoformat()}}},
                                                                 {"terms": {"category": ["flooding", "catch_basin"]}}]}},
                   aggs={"hot": {"significant_terms": {"field": "neighborhood", "size": 5, "min_doc_count": 3,
                                                       "background_filter": {"range": {"requested_at": {"gte": (now - timedelta(days=5 * 365)).isoformat()}}}}}})  # fmt: skip
    return {"hours": hours, "by_neighborhood": rows, "unusual": [{"neighborhood": b["key"], "reports": b["doc_count"]} for b in sig["aggregations"]["hot"]["buckets"]]}


def hotspots_now(s, hours: int = 6) -> list[dict]:
    """Blocks with recent reports joined to their 5-year storm history (ES|QL LOOKUP JOIN on a lookup-mode index)."""
    return s.esql(f"""FROM {s.ix("reports")}
| WHERE requested_at >= NOW() - TO_TIMEDURATION(CONCAT(TO_STRING(?hours), " hours")) AND category IN ("flooding", "catch_basin")
| STATS now = COUNT(*) BY h3_9
| LOOKUP JOIN {s.ix("profiles")} ON h3_9
| EVAL repeat_offender = storm_reports_5y >= 10
| KEEP h3_9, now, storm_reports_5y, in_100yr, neighborhood, repeat_offender
| SORT now DESC, storm_reports_5y DESC | LIMIT 15""", [{"hours": hours}])  # fmt: skip


def summary(s, hours: int = 24) -> str | None:
    """One-paragraph situation summary generated inside Elasticsearch by ES|QL COMPLETION (Mistral inference endpoint)."""
    try:
        rows = s.esql(f"""FROM {s.ix("reports")}
| WHERE requested_at >= NOW() - TO_TIMEDURATION(CONCAT(TO_STRING(?hours), " hours")) AND category IN {CATS}
| STATS n = COUNT(*) BY neighborhood, category | SORT n DESC | LIMIT 12
| EVAL line = CONCAT(COALESCE(neighborhood, "unknown"), ": ", TO_STRING(n), " ", category)
| STATS lines = VALUES(line)
| EVAL prompt = CONCAT("In 2 plain sentences for San Francisco residents, summarize these 311 storm-related report counts from the last ",
    TO_STRING(?hours), " hours. Use only these numbers: ", MV_CONCAT(lines, "; "))
| COMPLETION summary = prompt WITH {{ "inference_id": "{COMPLETION}" }}
| KEEP summary""", [{"hours": hours}])  # fmt: skip
        return rows[0]["summary"] if rows else None
    except Exception:  # noqa: BLE001 - endpoint missing locally / no Mistral key
        return None


def overview(s) -> dict:
    return {"citywide_24h": citywide(s, 24), "hotspots_6h": hotspots_now(s, 6), "summary_24h": summary(s, 24)}
