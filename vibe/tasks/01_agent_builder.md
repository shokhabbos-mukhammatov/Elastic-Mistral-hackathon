You are working in the HighGround SF repo (Python 3.12, functional style, no classes unless needed). Read AGENTS.md, highground/config.py, highground/store.py and highground/ops.py first.

Task: create `highground/agent_builder.py` that registers Elastic Agent Builder **ES|QL tools** and one **agent** in Kibana, so the same Elasticsearch facts are available to any MCP client (Vibe, Mistral agents) through `{HG_KIBANA_URL}/api/agent_builder/mcp?namespace=highground`.

Requirements:
- Use `httpx` and `settings()` from highground/config.py: `kibana_url`, `kibana_api_key` (header `Authorization: ApiKey <key>`), plus header `kbn-xsrf: true`. Index names come from `settings().ix("reports")` and `settings().ix("profiles")` (prefix `hn-`).
- Tools (ids prefixed `highground.`; idempotent: POST `/api/agent_builder/tools`, and if it already exists PUT `/api/agent_builder/tools/{id}` WITHOUT the `type` field). Each tool: `{"id", "type": "esql", "description", "tags": ["highground"], "configuration": {"query": ..., "params": {name: {"type": "integer"|"string", "description": ...}}}}`. Parameter types are `string`/`integer` (not `keyword`). Use `?name` placeholders. For durations use `NOW() - TO_TIMEDURATION(CONCAT(TO_STRING(?hours), " hours"))`.
  1. `highground.hotspots_now(hours)`: flooding/catch_basin reports in the last N hours grouped BY h3_9, then `LOOKUP JOIN <profiles> ON h3_9` to add storm_reports_5y, in_100yr, neighborhood; sort by count.
  2. `highground.neighborhood_reports(hours)`: storm-related reports (flooding, catch_basin, sewer_backup, tree) by neighborhood and category in the last N hours.
  3. `highground.storm_days(min_reports)`: days (Pacific time is fine as UTC date) with at least N flooding reports citywide, newest first, LIMIT 20.
  4. `highground.block_history(h3)`: flooding reports for one H3 cell by year, plus its profile row.
- Agent: id `highground-ops`, name "HighGround SF ops", instructions explaining it helps SF staff and residents understand where flooding is happening now vs historically, using only tool results, and tools `[{"tool_ids": [the four ids]}]`. POST `/api/agent_builder/agents`, PUT `/api/agent_builder/agents/{id}` if it exists. If agent creation fails (some deployments need security features), log it and keep the tools.
- `python -m highground.agent_builder` registers everything and then calls each tool once via POST `/api/agent_builder/tools/_execute` with `{"tool_id": ..., "tool_params": {...}}` and prints a short result summary. Also print the MCP URL.
- Verify for real: run `python -m highground.agent_builder` against the configured Kibana and fix errors until all four tools execute. Do not print or commit API keys.
