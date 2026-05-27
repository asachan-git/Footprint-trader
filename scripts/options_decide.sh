#!/usr/bin/env bash
# Auto-trigger /options/decide on a schedule during NSE market hours.
# Aligned to bar close + offset so chain data is fresh.
#
# Usage:
#   bash scripts/options_decide.sh                               # NIFTY 5m default
#   bash scripts/options_decide.sh --symbol BANKNIFTY --tf 1m
#   bash scripts/options_decide.sh --symbol NIFTY --tf 5m --interval 300

set -euo pipefail

SYMBOL="NIFTY"
TF="5m"
INTERVAL=300          # seconds; default = 5 minutes (match tf)
OFFSET=12             # seconds past bar close before calling decide
FLASK="http://localhost:5000"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/logs/options_decide.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --symbol)   SYMBOL="$2";   shift 2 ;;
    --tf)       TF="$2";       shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --offset)   OFFSET="$2";   shift 2 ;;
    --flask)    FLASK="$2";    shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$LOG")"
echo "[options-decide] $SYMBOL $TF every ${INTERVAL}s +${OFFSET}s offset → $FLASK"
echo "[options-decide] log → $LOG"

_is_market_hours() {
  # NSE: 9:15–15:30 IST = 03:45–10:00 UTC
  # Use Python for clean UTC minute arithmetic
  python3 -c "
import datetime
now = datetime.datetime.now(datetime.timezone.utc)
mins = now.hour * 60 + now.minute
print('1' if 225 <= mins < 600 else '0')
"
}

while true; do
  now=$(date +%s)
  next=$(( (now / INTERVAL + 1) * INTERVAL + OFFSET ))
  sleep $((next - now))

  if [[ "$(_is_market_hours)" != "1" ]]; then
    echo "[options-decide] $(date '+%H:%M:%S UTC') outside market hours — skipping"
    continue
  fi

  ts=$(date '+%Y-%m-%d %H:%M:%S')

  # /options/decide
  d=$(curl -s -X POST "$FLASK/options/decide" \
    -H "Content-Type: application/json" \
    -d "{\"symbol\":\"$SYMBOL\",\"tf\":\"$TF\"}")

  brief=$(python3 -c "
import sys, json
try:
    r = json.loads(sys.stdin.read())
    if not r.get('ok'):
        reason = r.get('skipped') or r.get('error') or 'unknown'
        print(f'skip: {str(reason)[:80]}')
    else:
        side = r.get('side', '?')
        opt = r.get('option_type', '')
        strike = r.get('strike', '')
        conf = r.get('confidence', 0)
        print(f'{side} {opt} {strike} conf={conf:.2f}')
except Exception as e:
    print(f'parse-err: {e}')
" <<< "$d" 2>/dev/null || echo "parse-err")

  line="$ts | $SYMBOL $TF | $brief"
  echo "[options-decide] $line"
  echo "$line" >> "$LOG"
done
