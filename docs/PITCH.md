# 2-minute pitch (numbers measured on Nov 3, 2026 against the loaded index)

**0:00 Who it helps (20 s).** Rosa rents a garage unit at 17th & Folsom, the low point of the Mission. 116 flood reports were filed on or next to her block in the last 5 years. She types the address *in Spanish* and in about 3 seconds gets her risk level, why, and what to do now, plus links to sfpuc.gov in Spanish.
*Demo:* briefing `lang=es`, then `lang=zh` ("narrative.by: mistral-large-4", receipts check: 0 dropped sentences).

**0:20 Novelty (35 s).** 311 learns about a flood once it's already happening. We replayed Dec 31, 2022 hour by hour from archived forecasts, rain and NWS products, with no look-ahead. Of the **178 blocks** that got flood reports that day, the engine flagged **113 as High at or before their first report**, a **median 53 minutes ahead**. The first flag came at 5:30 AM, 10 minutes before the first report citywide. Cost: 93 false-alarm blocks out of 526 flagged. The score is deterministic and auditable; Mistral only explains it, and any sentence with a number that isn't in the facts gets dropped.
*Demo:* replay slider; blocks turn red before the report pins land.

**0:55 Elastic (30 s).** One Elasticsearch project does everything:
- **Geo:** 28,862 flood-zone and sea-level-rise polygons. A `geo_match` **enrich pipeline** tags each of **160,770 SF311 reports** (9.2 years) with flood zone and neighborhood at ingest (4,410 fall inside the 100-year storm zone). `_mvt` H3 vector tiles go straight to the map, and a `search_as_you_type` geocoder covers 244,821 addresses.
- **ES|QL:** `LOOKUP JOIN` of "reports right now" against 748 block profiles built from 35 storm days, plus `COMPLETION`: Mistral writes the ops summary *inside* the ES|QL query.
- **Vector/semantic:** official guides as `semantic_text` on a Mistral-embeddings inference endpoint, searched with an RRF retriever (BM25 + semantic).
- **Agent Builder:** 4 ES|QL tools exposed over MCP.

**1:25 Mistral (30 s).**
- `mistral-large-4` runs the multilingual briefings and a function-calling agent over read-only ES tools. Asked in Spanish, it geocodes, pulls the briefing, searches guides and cites sfpuc.gov.
- **Vision + JSON:** a photo of a flooded street becomes a 311 draft ("flooding, water visible, severity high").
- **Moderation** runs before anything is drafted, and Voxtral handles voice reports.
- `mistral-embed` and `mistral-small` run as Elasticsearch inference endpoints.
- **Vibe CLI:** three headless Vibe runs, in parallel, wrote the Agent Builder module, 65 unit tests and the ingest worker (122 steps, 7.7M tokens; transcripts in `vibe-logs/`). Vibe then queried Elastic through the Agent Builder MCP server: "which blocks are repeat offenders?"

**1:55 Close (5 s).** "HighGround SF: the forecast knew, 311 lagged. Now your block knows first."

---
Numbers source: `/api/backtest/2022-12-31`, `_count` on each `hn-*` index, `/api/briefing?address=17th and Folsom`. Not an official alert service.
