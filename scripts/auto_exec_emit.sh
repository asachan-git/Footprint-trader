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

# ── Singleton lock, keyed by account+symbol ─────────────────────────────────────
# Two emitters on the same account+symbol means every bar close is emitted TWICE,
# so each setup arms twice — double the grids, double the exposure, and the second
# arm is invisible in the logs because it looks identical to the first. This must
# be impossible by construction, not by remembering not to do it: the watchdog
# relaunches on a pgrep miss, and pgrep pattern-matching on a command line is not
# a reliable exclusion.
_key=$(echo "${ACCOUNT}_${SYMBOL}" | tr -c 'A-Za-z0-9_' '_')
_LOCKDIR="${TMPDIR:-/tmp}/fb_emitter_${_key}.lock"
if ! mkdir "$_LOCKDIR" 2>/dev/null; then
    _owner=$(cat "$_LOCKDIR/pid" 2>/dev/null || echo "?")
    if kill -0 "$_owner" 2>/dev/null; then
        echo "[auto_exec_emit] already running for $ACCOUNT/$SYMBOL (pid $_owner) — exiting."
        exit 0
    fi
    echo "[auto_exec_emit] stale lock from pid $_owner — taking it over."
    rm -rf "$_LOCKDIR"; mkdir "$_LOCKDIR" || { echo "[auto_exec_emit] cannot lock"; exit 1; }
fi
echo $$ > "$_LOCKDIR/pid"
trap 'rm -rf "$_LOCKDIR"' EXIT INT TERM

# Per-TF setup lists:
#   1m  — disabled (see below)
#   5m  — hvn_inside_touch + hvn_displacement + hvn_edge + lvn_displacement + lvn_edge_touch
#   15m — hvn_inside_touch + hvn_displacement + hvn_edge + lvn_displacement + lvn_edge_touch
#   1h  — hvn_displacement
# hvn_edge reads the SAME daily/weekly VP the chart draws (vp_cache.get), so it arms
# on the HVN edge-touch you see on the chart — unlike hvn_inside_touch, which measures
# the rolling-window VP and a stricter close-inside+wick-reject geometry.
# squeeze dropped 2026-08-20: lost every single day it traded in the Jun22-29 study
# (-6,670, the worst/most consistent loser measured).
# lvn_displacement vs lvn_edge_touch — two DIFFERENT mechanisms, not the same setup under
# two names. lvn_displacement (existing since before base-v2's fork): fulcrum = LVN
# MIDPOINT, few legs planted on the LVN's own edges, bets on a fast one-way thrust out of
# the vacuum. lvn_edge_touch (ported 2026-08-21 from 3e04db8, 2026-07-07 upstream — never
# in base-v2, which forked 2 weeks earlier): fulcrum = LVN's OUTER EDGE, same
# closed-inside+wick-touch geometry as hvn_inside_touch, TP targets the near edge of the
# next HVN (conservative). The historical "+73.8/lot, most consistent single-setup
# winner" study window (Jun22-Jul20) overlaps lvn_edge_touch's Jul-7 introduction, so
# that figure may credit the edge-touch mechanism, not the displacement one — running
# both in parallel (independent magics, no conflict) until there's data to prefer one.
# Override per-TF with FB_SETUPS_1M / FB_SETUPS_5M / FB_SETUPS_15M env vars.
# 1m disabled by default 2026-08-20: negative on every tree ever measured (-72 USC/lot
# June, -11 July, 49% both-sided), and the fastest/most whipsaw-prone TF for a reversion
# setup breaking into a trend instead (-832.25 on magic 770011 was the trigger — edge=bottom
# expected a reversion up, price broke through and kept going). Set FB_SETUPS_1M to bring
# it back if ever wanted.
SETUPS_1M=(${FB_SETUPS_1M:-})
SETUPS_5M=(${FB_SETUPS_5M:-hvn_inside_touch hvn_displacement hvn_edge lvn_displacement lvn_edge_touch})
SETUPS_15M=(${FB_SETUPS_15M:-hvn_inside_touch hvn_displacement hvn_edge lvn_displacement lvn_edge_touch})
SETUPS_1H=(${FB_SETUPS_1H:-hvn_displacement})

emit_loop() {
  local tf="$1" interval="$2" offset="$3"
  shift 3
  local setups=("$@")
  echo "[emit:$tf] started — every ${interval}s (offset ${offset}s) → $SYMBOL acct $ACCOUNT setups=[${setups[*]-}]"
  while true; do
    local now next sleep_s
    now=$(date +%s)
    next=$(( (now / interval + 1) * interval + offset ))
    sleep_s=$((next - now))
    sleep "$sleep_s"
    local ts resp note hint
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    for hint in "${setups[@]+"${setups[@]}"}"; do
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

# Uniform 1m TP/order refresh: every 1m bar close, re-target EVERY active cycle (any TF)
# against the live HVN — so all orders track structure at a steady 1m cadence regardless
# of which TF armed them. Independent of the emit setups (refresh ≠ arm).
refresh_loop() {
  local interval=60 offset=5
  echo "[refresh] started — every ${interval}s → re-target all active cycles"
  while true; do
    local now next sleep_s ts resp n
    now=$(date +%s)
    next=$(( (now / interval + 1) * interval + offset ))
    sleep_s=$((next - now)); sleep "$sleep_s"
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    resp=$(curl -s -X POST "${FLASK}/exec/refresh_tps" \
      -H "Content-Type: application/json" \
      -d "{\"account\":\"${ACCOUNT}\",\"symbol\":\"${SYMBOL}\"}")
    n=$(echo "$resp" | python3 -c "
import sys,json
try: r=json.load(sys.stdin); print(len(r.get('refreshed_magics',[])))
except Exception: print('?')" 2>/dev/null || echo '?')
    echo "$ts [$SYMBOL refresh] re-targeted $n active cycle(s)"
  done
}

# Each TF runs an INDEPENDENT parallel cycle, isolated by strategy×TF magic.
# Offsets keep coincident closes off the same instant (avoids thundering-herd at :00).
emit_loop 15m 900   0  "${SETUPS_15M[@]}" &
P15=$!
emit_loop 5m  300  0  "${SETUPS_5M[@]}" &
P5=$!
emit_loop 1m  60    0  "${SETUPS_1M[@]+"${SETUPS_1M[@]}"}" &
P1=$!
emit_loop 1h  3600 0  "${SETUPS_1H[@]}" &
P1H=$!
refresh_loop &
PR=$!

echo "[auto_exec_emit] running — 15m($P15) 5m($P5) 1m($P1) 1h($P1H) refresh($PR). Ctrl-C to stop."
trap 'kill $P15 $P5 $P1 $P1H $PR 2>/dev/null; echo "[auto_exec_emit] stopped."; exit 0' INT TERM
wait
