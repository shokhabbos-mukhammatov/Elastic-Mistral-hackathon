#!/usr/bin/env bash
# Vibe talks to Elasticsearch through Elastic Agent Builder's MCP server (ES|QL tools registered by highground/agent_builder.py).
set -euo pipefail
cd "$(dirname "$0")/.."
vibe mcp add highground-elastic --url "${HG_KIBANA_URL%/}/api/agent_builder/mcp?namespace=highground" \
  --api-key-env HG_KIBANA_API_KEY --api-key-format "ApiKey {token}" || true
mkdir -p vibe-logs/mcp-demo
vibe -p "Use the highground-elastic MCP tools only. Which San Francisco blocks have the most flooding or storm-drain reports in the last 72 hours, and which of them are repeat offenders in past storms? Answer in 5 bullet points, then the same summary in Spanish." \
  --trust --auto-approve --max-turns 15 --output json --output-dir vibe-logs/mcp-demo | tee vibe-logs/mcp-demo/result.json
