#!/usr/bin/env bash
# Auto-trigger /decide every minute (aligned ~5s past minute close), then /label.
#
# Usage:
#   bash scripts/auto_decide.sh                                # BTCUSDT 1m by default
#   bash scripts/auto_decide.sh --symbol XAUUSDm --tf 1m
#   bash scripts/auto_decide.sh --interval 60 --offset 10

set -euo pipefail

SYMBOL="BTCUSDT"
TF="1m"
INTERVAL=60
OFFSET=10
FLASK="http://localhost:5000"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/logs/auto_decide.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --symbol) SYMBOL="$2"; shift 2 ;;
    --tf) TF="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --offset) OFFSET="$2"; shift 2 ;;
    --flask) FLASK="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$LOG")"
echo "[auto-decide] $SYMBOL $TF every ${INTERVAL}s @ +${OFFSET}s past close"
echo "[auto-decide] log → $LOG"

while true; do
  now=$(date +%s)
  next=$(( (now / INTERVAL + 1) * INTERVAL + OFFSET ))
  sleep $((next - now))

  ts=$(date '+%Y-%m-%d %H:%M:%S')

  # /decide (pre-filter gates Claude)
  d=$(curl -s -X POST "$FLASK/decide" \
    -H "Content-Type: application/json" \
    -d "{\"symbol\":\"$SYMBOL\",\"tf\":\"$TF\"}")
  brief=$(echo "$d" | python3 -c "
import sys,json
r=json.load(sys.stdin)
if r.get('skipped'): print(f'skip ({r[\"reason\"]})')
elif r.get('decision'): d=r['decision']; print(f\"{d['side']} conf={d['confidence']}\")
else: print('err: ' + str(r.get('error'))[:80])
" 2>/dev/null || echo "parse-err")

  # /label (walks forward outcomes for pending decisions)
  l=$(curl -s -X POST "$FLASK/label" -H "Content-Type: application/json" -d "{}" || true)
  labeled=$(echo "$l" | python3 -c "import sys,json; print(json.load(sys.stdin).get('labeled', 0))" 2>/dev/null || echo "?")

  line="$ts | decide: $brief | labeled: $labeled"
  echo "[auto-decide] $line"
  echo "$line" >> "$LOG"
done
