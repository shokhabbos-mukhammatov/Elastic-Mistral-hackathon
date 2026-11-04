# HighGround SF: notes for coding agents (Mistral Vibe, Claude Code)

Block-level flood & storm-hazard copilot for San Francisco. Python 3.12, FastAPI, Elasticsearch 9 (Serverless), Mistral.

- Style: functional core. Pure functions for logic (`risk.assess`, parsers, `replay.Replay.inputs`), thin I/O wrappers (`store.Store`). Add a class only for a held resource.
- The risk score is deterministic (`highground/risk.py`). Mistral explains it and handles unstructured input; it never decides the level.
- Index names always go through `settings().ix(name)` (prefix `hn-`). Never hard-code credentials; config comes from `HG_*` env vars (`highground/config.py`).
- No look-ahead in replay: inputs at time t only use data that existed at t.
- Run: `uvicorn app:app --reload`; load data: `python -m highground.ingest setup|layers|places|backfill 9.2|poll|profiles|guides|replay 2022-12-31`.
- Tests: `pytest -q`; lint: `ruff check .` (line length 200).
