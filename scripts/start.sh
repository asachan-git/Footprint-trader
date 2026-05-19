#!/usr/bin/env bash
# Single-command launcher — starts all services.
# Usage:
#   bash scripts/start.sh                  # BTC (Binance) + XAUT (Bybit)
#   bash scripts/start.sh --exness         # also stream XAUUSDm via Exness

set -euo pipefail
export TZ='Asia/Kolkata'   # all timestamps in IST (UTC+5:30)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

source venv/bin/activate

EXNESS=0
FLASK_URL="http://localhost:5000"
DECIDE_INTERVAL=900    # seconds between /decide_multi calls (default 15m = 900s)
DECIDE_TF="15m"        # TF for decisions (1m for testing, 15m for production)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exness) EXNESS=1; shift ;;
    --interval) DECIDE_INTERVAL="$2"; shift 2 ;;
    --tf) DECIDE_TF="$2"; shift 2 ;;
    *) shift ;;
  esac
done

mkdir -p logs

PIDS=()

cleanup() {
  echo ""
  echo "[start] stopping all services..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null
  echo "[start] all stopped."
}
trap cleanup INT TERM

# 0 — Rebuild real tick footprint history (runs in background, only downloads new days)
echo "[start] refreshing tick footprint history (background)..."
PYTHONPATH=. python3 scripts/rebuild_footprint_history.py --days 8 \
  --symbol BTCUSDT --symbol XAUTUSDT \
  > logs/rebuild_history.log 2>&1 &
PIDS+=($!)

# 1 — Flask
echo "[start] Flask server..."
PYTHONPATH=. python3 -m server.app > logs/flask.log 2>&1 &
PIDS+=($!)
sleep 2

# 2 — BTCUSDT via Bybit linear perpetual (confirmed working from India)
echo "[start] BTCUSDT ingress (Bybit)..."
PYTHONPATH=. python3 -m bybit.main \
  --symbol BTCUSDT --tf 1m --price-step 10.0 --category linear \
  > logs/bybit_btc.log 2>&1 &
PIDS+=($!)

# 3 — XAUTUSDT (Tether Gold, Bybit spot)
echo "[start] XAUTUSDT ingress (Bybit spot)..."
PYTHONPATH=. python3 -m bybit.main \
  --symbol XAUTUSDT --tf 1m --price-step 0.1 --category spot \
  > logs/bybit_xaut.log 2>&1 &
PIDS+=($!)

# 4 — Exness XAUUSDm optional
if [[ $EXNESS -eq 1 ]]; then
  echo "[start] Exness XAUUSDm ingress..."
  PYTHONPATH=. python3 -m exness.main \
    --symbol XAUUSDm --tf 1m --price-step 0.1 \
    > logs/exness.log 2>&1 &
  PIDS+=($!)
fi

# 5 — combined multi-symbol decision
sleep 3
echo "[start] auto-decide: BTCUSDT + XAUTUSDT combined (every ${DECIDE_INTERVAL}s)..."

auto_decide_multi() {
  local flask="$1"
  local interval="$2"
  local tf="$3"
  local log="logs/decide_multi.log"
  local offset=5
  echo "[multi-decide] started — BTC+XAUT every ${interval}s"
  while true; do
    local now next sleep_s
    now=$(date +%s)
    if [[ $interval -ge 60 ]]; then
      next=$(( (now / interval + 1) * interval + offset ))
      sleep_s=$((next - now))
    else
      sleep_s=$interval
    fi
    sleep $sleep_s
    local ts result note
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    result=$(curl -s -X POST "${flask}/decide_multi" \
      -H "Content-Type: application/json" \
      -d "{\"symbols\":[\"BTCUSDT\",\"XAUTUSDT\"],\"tf\":\"${tf}\"}")
    note=$(echo "$result" | python3 -c "
import sys, json
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
def ist(ts): return datetime.fromtimestamp(ts, tz=IST).strftime('%H:%M IST') if ts else ''

r = json.load(sys.stdin)
if not r.get('ok'):
    print('  ERR: ' + str(r.get('error',''))[:100])
    exit()

print('  ' + '-'*65)
for d in r.get('results', []):
    dec = d.get('decision', {})
    side = dec['side'].upper()
    conf = dec.get('confidence', 0)
    rat  = dec.get('rationale', '')[:90]
    inv  = dec.get('invalidation_note', '')[:60]
    val  = d.get('validator_reason', '')
    sym  = d.get('symbol', '')
    e    = dec.get('entry')
    sl   = dec.get('stop_loss')
    tp   = dec.get('take_profit')
    risk = abs(e - sl) if e and sl else 0
    rr   = abs(tp - e) / risk if risk > 0 else 0
    sid_icon = '📈' if side=='LONG' else '📉' if side=='SHORT' else '—'
    trade_line = f'entry={e:.2f} SL={sl:.2f} TP={tp:.2f} R:R={rr:.1f}' if e else ''
    print(f'  {sid_icon} {sym:12} {side:6} conf={conf:.2f}  {trade_line}')
    if rat: print(f'     → {rat}')
    if inv: print(f'     ⚠ exit if: {inv}')
    if val: print(f'     ✗ rejected: {val}')

cross = r.get('cross_market_note', '')
if cross: print(f'  🔗 {cross[:110]}')
" 2>/dev/null || echo "  parse err")
    echo "$ts | $note" | tee -a "$log"
  done
}

export -f auto_decide_multi
bash -c "auto_decide_multi '$FLASK_URL' '$DECIDE_INTERVAL' '$DECIDE_TF'" > logs/decide_multi.log 2>&1 &
PIDS+=($!)

echo ""
echo "[start] ✓ All services running. PIDs: ${PIDS[*]}"
echo ""
echo "  Logs:"
echo "    Flask:           logs/flask.log"
echo "    Bybit BTC:       logs/bybit_btc.log"
echo "    Bybit XAUT:      logs/bybit_xaut.log"
echo "    Multi-decide:    logs/decide_multi.log"
echo "    History rebuild: logs/rebuild_history.log"
[[ $EXNESS -eq 1 ]] && echo "    Exness:          logs/exness.log"
echo ""
echo "  Commands:"
echo "    curl -s http://localhost:5000/stats | python3 -m json.tool"
echo "    curl -s 'http://localhost:5000/footprint?symbol=BTCUSDT&tf=1m'"
echo ""
echo "[start] Ctrl+C to stop all."
echo ""

# live tail of decision log
touch logs/decide_multi.log
tail -f logs/decide_multi.log &
PIDS+=($!)

wait
