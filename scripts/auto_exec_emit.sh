#!/usr/bin/env bash
# auto_exec_emit.sh — fire the structural-grid emitter on each 5m + 15m candle close.
#
# On every bar boundary it POSTs /exec/emit_grid {account, symbol, tf, trigger_hint};
# the server builds the neutral grid for the best "structural" trigger (HVN-edge touch
# OR VP-level touch: POC/VAH/VAL/naked-POC/LVN), rebases onto the EA's reported venue
# quote, and enqueues CANCEL_PENDINGS/PLACE_PENDING commands the EA drains.
# Server-side fulcrum dedup → at most one grid per touched-level episode (not every bar).
#
# DEMO ONLY: the grid family is sim-NEGATIVE. This is for watching the live
# placement/lifecycle on a demo account, not for edge.
#
# Usage:
#   scripts/auto_exec_emit.sh [FLASK_URL] [ACCOUNT] [SYMBOL]
#   scripts/auto_exec_emit.sh http://127.0.0.1:5000 25230425 XAUUSD+
set -u

FLASK="${1:-http://127.0.0.1:5000}"
ACCOUNT="${2:?account login required (e.g. 25230425)}"
SYMBOL="${3:-XAUUSD+}"

# Parallel grid setups — each posts its OWN trigger_hint and (post-P1 magic re-key) arms
# an INDEPENDENT cycle on the same symbol+TF. Override the list per instance with the
# FB_SETUPS env var (space-separated) — e.g. live deploys a vetted subset while demo keeps
# all three for data:  FB_SETUPS="hvn_inside_touch" scripts/auto_exec_emit.sh ...
SETUPS=(${FB_SETUPS:-hvn_inside_touch squeeze vp_levels})

emit_loop() {
  local tf="$1" interval="$2" offset="$3"
  echo "[emit:$tf] started — every ${interval}s (offset ${offset}s) → $SYMBOL acct $ACCOUNT"
  while true; do
    local now next sleep_s
    now=$(date +%s)
    next=$(( (now / interval + 1) * interval + offset ))
    sleep_s=$((next - now))
    sleep "$sleep_s"
    local ts resp note hint
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    # Fire each setup independently — each arms its own magic cycle (P1), so all three
    # can be live at once on this symbol+TF.
    for hint in "${SETUPS[@]}"; do
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
      echo "$ts [$SYMBOL $tf/$hint] $note"
    done
  done
}

# Each TF runs an INDEPENDENT parallel cycle (server keys cycles by TF, isolates by
# strategy×TF magic) — they no longer contend for the symbol. Offsets keep coincident
# closes off the same instant (avoids a thundering-herd at :00). Override the TF set
# per instance with FB_TFS (space-separated, from: 1h 15m 5m 1m) — same pattern as
# FB_SETUPS above, e.g.  FB_TFS="1m 5m 15m" scripts/auto_exec_emit.sh ...
TFS=(${FB_TFS:-1h 15m 5m 1m})
PIDS=()
for tf in "${TFS[@]}"; do
  case "$tf" in
    1h)  emit_loop 1h  3600 20 & ;;
    15m) emit_loop 15m 900  8  & ;;
    5m)  emit_loop 5m  300  12 & ;;
    1m)  emit_loop 1m  60   3  & ;;
    *) echo "[auto_exec_emit] unknown TF '$tf' in FB_TFS, skipping" >&2; continue ;;
  esac
  PIDS+=($!)
done

echo "[auto_exec_emit] running — ${TFS[*]} (pids: ${PIDS[*]}). Ctrl-C to stop."
trap 'kill "${PIDS[@]}" 2>/dev/null; echo "[auto_exec_emit] stopped."; exit 0' INT TERM
wait
