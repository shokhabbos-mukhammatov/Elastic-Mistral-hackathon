# Hack-night handoff (for the next session, which has the keys)

All code here was written during the hack night (after 5:30 PM PT, Nov 3, 2026). The build session had **no Mistral or Elastic Cloud keys**, so everything below "Done" was tested only against a local Elasticsearch 9.5. Mistral code paths are written but untested.

## Done and tested locally
- Ingest: 160k DataSF 311 reports (9.2 years), 28k flood/sea-level-rise polygons plus 41 neighborhoods, 245k places, live Open311/NWS/NOAA/CHP feeds.
- Elastic features: geo_match enrich pipeline, `_mvt` tiles, ES|QL `LOOKUP JOIN`, `significant_terms`, guides with BM25.
- Risk engine and briefing, with a template fallback when Mistral is unavailable.
- Replay and backtest for 2022-12-31: 178 blocks with flood reports; 113 flagged High at or before their first report; median lead 53 min; 93 false-alarm blocks out of 526 flagged.
- Web UI: briefing, map, replay slider, ask, report.

## Next session, in order (about 45 min)
1. `pip install -r requirements.txt mistral-vibe`.
   - Check that `HG_ES_URL`, `HG_ES_API_KEY`, `HG_KIBANA_URL`, `HG_KIBANA_API_KEY`, `HG_MISTRAL_API_KEY` and `MISTRAL_API_KEY` are set.
   - Run `curl -s -H "Authorization: Bearer $HG_MISTRAL_API_KEY" https://api.mistral.ai/v1/models` and confirm that `mistral-large-4`, `mistral-small-latest`, `mistral-embed`, `mistral-moderation-2603` and `voxtral-mini-latest` exist. Fix `highground/config.py` defaults if they don't.
2. Load Elastic Cloud: `for c in setup layers places "backfill 9.2" poll profiles guides "replay 2022-12-31"; do python -m highground.ingest $c; done`.
   - `setup` creates the Mistral inference endpoints `mistral-embeddings`, `mistral-chat` and `mistral-completion`, so `guides.content` becomes `semantic_text`.
   - If `hn-guides` was created before the endpoint existed, delete the index and run `setup` + `guides` again.
   - The `replay` command rebuilds `data/replay/2022-12-31.json` against cloud profiles. The committed file already works.
3. **Mistral Vibe** (a judging criterion):
   - Run `scripts/vibe_build.sh`. Vibe writes `highground/agent_builder.py`, `tests/*` and `worker.py` headless, in parallel, and leaves transcripts and costs in `vibe-logs/<task>/`.
   - Review the diffs. Run `pytest -q` and `python -m highground.agent_builder`.
   - Then run `scripts/vibe_mcp_demo.sh`: Vibe queries Elastic through the Agent Builder MCP server. Commit `vibe-logs/`.
4. Test Mistral for real, and fix anything that breaks:
   - `/api/briefing?address=17th and Folsom&lang=es` and `lang=zh`: check that `narrative.by` is the model and `dropped` is short.
   - `/api/ask` in Spanish: the agent should call tools and cite a sfpuc.gov URL.
   - `/api/report` with a photo of a flooded street: check `vision` and the category.
   - `/api/ops`: `summary_24h` comes from ES|QL COMPLETION.
5. Demo hosting: the simplest option is `uvicorn app:app` on the presenter's laptop, using the same `.env`. Railway is optional.
6. Commit and push to `claude/gallant-clarke-466wue`, then merge to `main` if the user asks.

## 2-minute pitch, mapped to the judging criteria
1. **Who it helps.** Rosa rents a garage unit at 17th & Folsom, the city's lowest point, where 116 flood reports were filed on or next to this block in 5 years. Open the app, type the address in Spanish, and get the risk, why, and what to do. *(Mistral multilingual + receipts check; Elastic geo enrich.)*
2. **Novelty.** Replay Dec 31, 2022: the forecast knew, 311 lagged. Drag the slider and blocks turn red before the reports arrive. The engine flagged 113 of 178 flooded blocks before their first report, a median 53 minutes ahead, measured with no look-ahead.
3. **Elastic.**
   - geo_shape enrich at ingest, `_mvt` H3 tiles
   - ES|QL LOOKUP JOIN of "now" against 5-year history
   - COMPLETION with Mistral inside ES
   - semantic_text with Mistral embeddings and an RRF retriever
   - Agent Builder tools exposed over MCP
4. **Mistral.**
   - large-4 function-calling agent
   - vision photo to a 311 draft
   - moderation
   - Voxtral
   - **Vibe CLI built three modules headless**; show `vibe-logs/` and the MCP demo where Vibe asks Elastic "which blocks are repeat offenders?"
