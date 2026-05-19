#!/usr/bin/env bash
# Clear all runtime data and start fresh.
# Keeps: config, prompts, code, venv.
# Clears: footprint bars, decisions, outcomes, logs.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "This will DELETE all stored bars, decisions, outcomes, and logs."
read -r -p "Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 0; }

# data
rm -f data/footprint/*.jsonl
rm -f data/decisions.jsonl
rm -f data/outcomes.jsonl
rm -f data/datasets/*.jsonl
rm -f data/raw/*.png data/raw/*.json

# logs
rm -f logs/*.log logs/*.jsonl

echo "Cleared."
echo ""
echo "Current mode: $(grep '^mode:' config/settings.yaml)"
echo "To switch to paper: edit config/settings.yaml → mode: paper"
echo "Then run: bash scripts/start.sh"
