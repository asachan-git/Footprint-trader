#!/usr/bin/env bash
# auto_exec_emit.sh — fire the HVN-grid emitter on each 5m + 15m candle close.
#
# On every bar boundary it POSTs /exec/emit_grid {account, symbol, tf}; the server
# builds the neutral grid (trigger_hint hvn_inside_touch), rebases it onto the EA's
# reported venue quote, and enqueues PLACE_PENDING/CLOSE_ALL commands the EA drains.
# Server-side dedup means at most one grid per touched-edge episode (not every bar).
#
# DEMO ONLY: hvn_inside_touch is sim-NEGATIVE. This is for watching the live
# placement/lifecycle on a demo account, not for edge.
#
# Usage:
#   scripts/auto_exec_emit.sh [FLASK_URL] [ACCOUNT] [SYMBOL]
#   scripts/auto_exec_emit.sh http://127.0.0.1:5000 25230425 XAUUSD+
set -u

FLASK="${1:-http://127.0.0.1:5000}"
ACCOUNT="${2:?account login required (e.g. 25230425)}"
SYMBOL="${3:-XAUUSD+}"

emit_loop() {
  local tf="$1" interval="$2" offset="$3"
  echo "[emit:$tf] started — every ${interval}s (offset ${offset}s) → $SYMBOL acct $ACCOUNT"
  while true; do
    local now next sleep_s
    now=$(date +%s)
    next=$(( (now / interval + 1) * interval + offset ))
    sleep_s=$((next - now))
    sleep "$sleep_s"
    local ts resp
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    resp=$(curl -s -X POST "${FLASK}/exec/emit_grid" \
      -H "Content-Type: application/json" \
      -d "{\"account\":\"${ACCOUNT}\",\"symbol\":\"${SYMBOL}\",\"tf\":\"${tf}\"}")
    local note
    note=$(echo "$resp" | python3 -c "
import sys, json
try: r = json.load(sys.stdin)
except Exception: print('parse-err'); sys.exit()
v = r.get('verdict','?')
if v == 'arm':
    print(f\"ARM n/side={r.get('n_per_side')} fulcrum={r.get('fulcrum')} venue_mid={r.get('venue_mid')} cmds={r.get('commands_enqueued')}\")
else:
    print(f\"skip ({r.get('skip_reason','')})\")
" 2>/dev/null || echo "err: $(echo "$resp" | head -c 120)")
    echo "$ts [$tf] $note"
  done
}

# 15m fires on the :00/:15/:30/:45 boundary; 5m a few seconds later when they coincide,
# so a 15m grid wins the dedup at a shared close. Offsets keep them off the same instant.
emit_loop 15m 900 8 &
P15=$!
emit_loop 5m 300 12 &
P5=$!

echo "[auto_exec_emit] running — 15m(pid $P15) + 5m(pid $P5). Ctrl-C to stop."
trap 'kill $P15 $P5 2>/dev/null; echo "[auto_exec_emit] stopped."; exit 0' INT TERM
wait
