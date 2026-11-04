#!/usr/bin/env bash
# Mistral Vibe writes three modules headless, in parallel; transcripts + cost go to vibe-logs/<task>/ (export.json + session journal).
# Needs MISTRAL_API_KEY in the environment and `pip install mistral-vibe`.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in vibe/tasks/*.md; do
  t=$(basename "$f" .md)
  mkdir -p "vibe-logs/$t"
  vibe --prompt-file "$f" --auto-approve --trust --max-price 2 --max-turns 60 --time-limit 1500 \
       --output json --output-dir "vibe-logs/$t" > "vibe-logs/$t/result.json" 2> "vibe-logs/$t/stderr.log" &
done
wait
for d in vibe-logs/*/; do echo "== $d"; python3 -c "import json,sys; e=json.load(open('$d/export.json')); print({k: e.get(k) for k in ('outcome','stop_reason','cost','usage')})" 2>/dev/null || true; done
