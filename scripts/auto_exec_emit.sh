#!/usr/bin/env bash
# auto_exec_emit.sh — HVN-edge tick touch + bar-close squeeze/vp_levels emitter.
#
# hvn_edge:  tick-poll every 5s — arms the moment live price touches an HVN boundary
#            (within width×0.5). One grid per magic: dedup:same_fulcrum + position_open
#            prevent re-arming until the current cycle closes.
# squeeze / vp_levels: bar-close loops (1m/5m/15m) unchanged — candle-close semantics.
#
# Usage:
#   scripts/auto_exec_emit.sh [FLASK_URL] [ACCOUNT] [SYMBOL]
#   scripts/auto_exec_emit.sh http://127.0.0.1:5000 25230425 XAUUSD+
set -u

FLASK="${1:-http://127.0.0.1:5000}"
ACCOUNT="${2:?account login required (e.g. 25230425)}"
SYMBOL="${3:-XAUUSD.pc}"

# Bar-close setups (candle-close semantics):
#   squeeze (strat 2), vp_levels (7/3), hvn_inside_touch (strat 1), lvn_close (strat 6).
# hvn_edge (strat 5) stays in the tick loop below (tick-speed trigger).
SETUPS=(squeeze vp_levels hvn_inside_touch lvn_close)

_emit_one() {
  local tf="$1" hint="$2"
  local resp note
  resp=$(curl -s -X POST "${FLASK}/exec/emit_grid" \
    -H "Content-Type: application/json" \
    -d "{\"account\":\"${ACCOUNT}\",\"symbol\":\"${SYMBOL}\",\"tf\":\"${tf}\",\"trigger_hint\":\"${hint}\"}")
  note=$(echo "$resp" | python3 -c "
import sys, json
try: r = json.load(sys.stdin)
except Exception: print('parse-err'); sys.exit()
v = r.get('verdict','?')
if v == 'arm':
    print(f\"ARM {r.get('trigger_kind','')} n/side={r.get('n_per_side')} fulcrum={r.get('fulcrum')} cmds={r.get('commands_enqueued')}\")
else:
    print(f\"skip ({r.get('skip_reason','')})\")
" 2>/dev/null || echo "err: $(echo "$resp" | head -c 120)")
  echo "$(date '+%Y-%m-%d %H:%M:%S') [$SYMBOL $tf/$hint] $note"
}

emit_loop() {
  local tf="$1" interval="$2" offset="$3"
  echo "[emit:$tf] started — every ${interval}s (offset ${offset}s) → $SYMBOL acct $ACCOUNT"
  while true; do
    local now next sleep_s
    now=$(date +%s)
    next=$(( (now / interval + 1) * interval + offset ))
    sleep_s=$((next - now))
    sleep "$sleep_s"
    local hint
    for hint in "${SETUPS[@]}"; do
      _emit_one "$tf" "$hint"
    done
  done
}

# hvn_edge tick-poll: fires every 5s on all active TFs.
# The detector returns None (→ skip) unless live price is within width×0.5 of an HVN edge,
# so this loop is nearly free when price is mid-range.
tick_loop() {
  local tf="$1"
  echo "[tick:$tf/hvn_edge] started — every 5s → $SYMBOL acct $ACCOUNT"
  while true; do
    sleep 5
    _emit_one "$tf" "hvn_edge"
  done
}

# Each TF runs an INDEPENDENT parallel cycle (server keys cycles by TF, isolates by
# strategy×TF magic) — they no longer contend for the symbol. Offsets keep coincident
# closes off the same instant (avoids a thundering-herd at :00).
tick_loop 1m  &
PT1=$!
tick_loop 5m  &
PT5=$!
tick_loop 15m &
PT15=$!

emit_loop 15m 900  8 &
P15=$!
emit_loop 5m  300  12 &
P5=$!
emit_loop 1m  60   3 &
P1=$!

# TP refresh loop — every 120s recompute HVN targets and enqueue MODIFY_TP for any
# active cycle whose tp_up/tp_down shifted by ≥1pt (rolling VP tracks intraday HVN drift).
tp_refresh_loop() {
  echo "[tp_refresh] started — every 120s → $SYMBOL acct $ACCOUNT"
  while true; do
    sleep 120
    local ts resp
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    resp=$(curl -s -X POST "${FLASK}/exec/tp_refresh" \
      -H "Content-Type: application/json" \
      -d "{\"account\":\"${ACCOUNT}\",\"symbol\":\"${SYMBOL}\",\"min_shift\":1.0}")
    local note
    note=$(echo "$resp" | python3 -c "
import sys, json
try: r = json.load(sys.stdin)
except Exception: print('parse-err'); sys.exit()
print(f\"refreshed={r.get('refreshed',0)} skipped={r.get('skipped',0)}\")
" 2>/dev/null || echo "err")
    echo "$ts [tp_refresh] $note"
  done
}
tp_refresh_loop &
PTP=$!

echo "[auto_exec_emit] running — tick-hvn_edge:1m($PT1) 5m($PT5) 15m($PT15) | bar-close:1m($P1) 5m($P5) 15m($P15) | tp_refresh($PTP). Ctrl-C to stop."
trap 'kill $PT1 $PT5 $PT15 $P15 $P5 $P1 $PTP 2>/dev/null; echo "[auto_exec_emit] stopped."; exit 0' INT TERM
wait
