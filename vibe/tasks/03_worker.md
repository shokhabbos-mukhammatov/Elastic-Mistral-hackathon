You are working in the HighGround SF repo. Read AGENTS.md, highground/store.py (run_feed), highground/feeds.py (FEEDS), highground/replay.py (build_profiles) and app.py.

Task: create `worker.py` at the repo root: the always-on ingest process.
- APScheduler `BlockingScheduler`. For every entry in `feeds.FEEDS` schedule `store.run_feed(store.Store(), name, http)` at its interval (seconds), first run immediately, `max_instances=1`, `coalesce=True`. One shared `feeds.client()`.
- Every 6 hours rebuild live profiles: `replay.build_profiles(s, now, index="profiles")`.
- Single-instance lease in Elasticsearch: index `settings().ix("locks")`, doc id `worker`, created with `op_type=create` and an `expires_at` 120 s ahead, renewed every 30 s by the holder; a second worker waits until the lease expires. Release it on SIGTERM/SIGINT.
- Log one line per job result with the standard `logging` module. Never crash on a feed error (run_feed already records errors).
- `python worker.py --once` runs every feed once and exits (used in CI and the demo). Test it for real against the configured Elasticsearch and show `/status` freshness afterwards.
