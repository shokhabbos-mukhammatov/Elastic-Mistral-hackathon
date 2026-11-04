# HighGround SF

**"Is my block going to flood or be dangerous in the next few hours, and what should I do?"**, answered for any San Francisco address, in the resident's language, with every number traced to its source.

Built at the Elastic × Mistral SF Hack Night (Nov 3, 2026).

## Why

- About 32,000 street-flood reports have reached SF311 since 2008. They cluster on a few storm days (560 on Dec 31, 2022) and on the same blocks every time: 17th & Folsom, Cayuga Ave, Gilman Ave.
- In 2022 the National Weather Service issued a Flood Watch about 4 days ahead, but the city planned for under 1 inch and got about 5.5. No one turned the regional forecast into "your block floods at this much rain."
- On Dec 31, 2022, about 2.3 in. of rain had already fallen by 9 AM, yet only 17 people had reported flooding. 311 learns about a flood only once it is happening.
- On storm days, case closure is 37% slower and about 30% of flood cases are duplicates or "nothing found."

Full research with sources: [docs/RESEARCH.md](docs/RESEARCH.md). Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What it does

1. **Block briefing.** Type an address or intersection and get a risk level (Low, Elevated, High or Severe), the reasons, what to do now, and help links. A deterministic engine computes the score; Mistral explains it in the resident's language, and a receipts check drops any sentence with a number that isn't in the facts.
2. **Storm replay and backtest.** Replays Dec 31, 2022 hour by hour from real archived data, and the engine sees nothing from the future. **Measured result:** 113 of the 178 blocks with flood reports were flagged High before (or when) their first report came in, with a **median lead of 53 minutes**.
3. **Ask anything, in any language.** A Mistral function-calling agent works over Elasticsearch tools: geocode, block briefing, official-guide search, and the citywide picture.
4. **Report a hazard.** A photo or text report goes through moderation, Mistral vision classification and an "already reported nearby?" check, then becomes a SF311 draft. Nothing is sent without the resident.
5. **City-ops view.** ES|QL `LOOKUP JOIN` of live reports against each block's 5-year storm history, `significant_terms` for unusual neighborhoods, and an ES|QL `COMPLETION` summary written by Mistral inside Elasticsearch. The same queries are exposed as **Elastic Agent Builder** tools over MCP.

## Elastic

| Feature | Where |
|---|---|
| `geo_shape` flood zones and a **geo_match enrich ingest pipeline** that tags every 311 report with its 100-year flood zone, sea-level-rise zone and neighborhood | `store.enrich_pipeline` |
| `geo_distance`, `geo_shape` point queries, `msearch` | `briefing.live_inputs` |
| **Vector tiles** (`_mvt`) with an H3 `geohex` aggregation, served straight to MapLibre | `app.tiles` |
| `search_as_you_type` geocoder over 245k SF addresses and intersections | `layers.geocode` |
| **ES\|QL**: STATS, **LOOKUP JOIN** (lookup-mode `profiles` index), **COMPLETION** with a Mistral inference endpoint | `ops.py` |
| `significant_terms`, `date_histogram`, terms aggregations | `ops.citywide`, `replay.build_profiles` |
| **Mistral inference endpoints** (`text_embedding`, `chat_completion`, `completion`) and **`semantic_text`** guides with an **RRF retriever** (BM25 + semantic) | `store.setup`, `guides.search` |
| **Agent Builder** ES\|QL tools and agent, plus its MCP server used by Mistral Vibe | `agent_builder.py`, `scripts/vibe_mcp_demo.sh` |

## Mistral

| Feature | Where |
|---|---|
| `mistral-large-4` multilingual briefings with a receipts check | `briefing.narrate` |
| Function calling: an agent over read-only ES tools | `agent.py` |
| Vision + JSON output: hazard photo to a 311 category and description | `report.py` |
| Moderation (`mistral-moderation-2603`) and Voxtral transcription | `report.py` |
| `mistral-embed` and chat models inside Elasticsearch (inference API) | `store.setup`, `ops.summary` |
| **Mistral Vibe CLI** wrote modules headless (transcripts and costs in `vibe-logs/`) and queried Elastic through Agent Builder MCP | `vibe/tasks/`, `scripts/vibe_*.sh` |

## Run it

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

Put the `HG_*` variables from [.env.example](.env.example) in `.env` (an Elastic Cloud Serverless URL and API key, plus a Mistral key). Then load the data:

```bash
for c in setup layers places "backfill 9.2" poll profiles guides "replay 2022-12-31"; do python -m highground.ingest $c; done
```

```bash
uvicorn app:app --port 8000
```

Open http://localhost:8000.

Data: SF311 Open311 and DataSF 311 (`vw6y-z8j6`), SFPUC 100-year storm flood map (`jzu3-4yxp`), sea-level-rise layer (`esku-ejgv`), neighborhoods, EAS addresses, NWS, IEM VTEC archive, NOAA CO-OPS 9414290, Open-Meteo, CHP.

*This is not an official alert service. Call 911 in an emergency. For official alerts, use AlertSF.*
