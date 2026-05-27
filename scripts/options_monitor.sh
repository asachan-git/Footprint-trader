#!/usr/bin/env bash
# Options position monitor — exits when underlying hits SL/TP or MIS time limit.
# Run alongside options_decide.sh.
#
# Usage:
#   bash scripts/options_monitor.sh
#   bash scripts/options_monitor.sh --interval 20

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INTERVAL=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

source venv/bin/activate 2>/dev/null || true

echo "[options-monitor] starting (interval=${INTERVAL}s)"
python3 -m options.monitor --interval "$INTERVAL"
