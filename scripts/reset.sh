#!/usr/bin/env bash
# Clear runtime state. Footprint bars are KEPT (expensive to re-fetch).
#
# Usage:
#   bash scripts/reset.sh           # clear decisions/positions/logs only (DEFAULT)
#   bash scripts/reset.sh --bars    # also delete footprint bars (full reset)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DELETE_BARS=0
[[ "${1:-}" == "--bars" ]] && DELETE_BARS=1

if [[ $DELETE_BARS -eq 1 ]]; then
  echo "FULL reset: deletes bars, decisions, outcomes, logs, VP cache."
else
  echo "Soft reset: clears decisions, positions, outcomes, logs."
  echo "Footprint bars (data/footprint/) and VP cache PRESERVED."
fi
read -r -p "Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 0; }

# Always clear
rm -f data/decisions.jsonl
rm -f data/outcomes.jsonl
rm -f data/positions.jsonl
rm -f data/datasets/*.jsonl
rm -f data/raw/*.png data/raw/*.json
rm -f logs/*.log logs/*.jsonl

# Only on --bars
if [[ $DELETE_BARS -eq 1 ]]; then
  rm -f data/footprint/*.jsonl
  rm -f data/vp_cache.json
  rm -f data/vp_history.jsonl
  echo "Bars + VP cache also cleared."
fi

echo "Done."
echo ""
echo "Current mode: $(grep '^mode:' config/settings.yaml)"
if [[ $DELETE_BARS -eq 1 ]]; then
  echo "Run: python3 scripts/fetch_history.py --days 5  (re-fetch bars)"
fi
echo "Then: bash scripts/start.sh"
