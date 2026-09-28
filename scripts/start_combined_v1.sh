#!/usr/bin/env bash
# start_combined_v1.sh — the minimal stack for the combined-v1 grid on one MT5 account.
#
#   bash scripts/start_combined_v1.sh <ACCOUNT> [SYMBOL]
#   bash scripts/start_combined_v1.sh 24678823 XAUUSD.pc
#
# Starts, in order:
#   1. Flask server (server.app) on :5000          → logs/flask.log
#   2. XAUT 1m footprint feed (Binance XAUUSDT perp, stored as XAUTUSDT)
#                                                  → logs/binance_xaut.log
#   3. Emitter watchdog → auto_exec_emit.sh, 15m hvn_inside_touch ONLY
#                                                  → logs/exec_emit_run.log, logs/emitter_watchdog.log
#
# Deliberately NOT started (scripts/start.sh runs them): the BTC feed, the Claude
# /decide_multi loop and the paper /grid_tick loop — none of them are part of combined-v1.
#
# Arming itself happens inside Flask on every EA poll (hvn_inside_touch, lvn_edge_touch,
# hvn_edge on 15m per config/settings.yaml). The emitter adds the 15m bar-close
# emit/refresh and the 5-minute TP refresh. Ctrl-C stops everything this script started.
set -uo pipefail
ACCOUNT="${1:?usage: start_combined_v1.sh <ACCOUNT> [SYMBOL]}"
SYMBOL="${2:-XAUUSD.pc}"
FLASK_URL="http://127.0.0.1:${FLASK_PORT:-5000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
[ -d venv ] && source venv/bin/activate
export PYTHONPATH="$ROOT"

# Emitter setups — exported so the watchdog's relaunches inherit them.
export FB_SETUPS_1M="" FB_SETUPS_3M="" FB_SETUPS_5M="" FB_SETUPS_10M="" FB_SETUPS_1H=""
export FB_SETUPS_15M="hvn_inside_touch"

PIDS=()
cleanup() {
  echo; echo "[combined-v1] stopping..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  pkill -f "auto_exec_emit.sh .*${ACCOUNT}" 2>/dev/null || true
  wait 2>/dev/null; echo "[combined-v1] stopped."
}
trap cleanup INT TERM

if lsof -nP -iTCP:"${FLASK_PORT:-5000}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[combined-v1] Flask already listening on ${FLASK_URL} — reusing it"
else
  echo "[combined-v1] starting Flask..."
  python3 -m server.app > logs/flask.log 2>&1 &
  PIDS+=($!)
fi
for _ in $(seq 1 30); do
  curl -s --max-time 2 "${FLASK_URL}/health" >/dev/null && break
  sleep 1
done
curl -s --max-time 2 "${FLASK_URL}/health" >/dev/null || { echo "[combined-v1] Flask did not come up — see logs/flask.log"; cleanup; exit 1; }
echo "[combined-v1] Flask up at ${FLASK_URL}"

echo "[combined-v1] starting XAUT feed..."
python3 -m binance.main --symbol XAUUSDT --symbol-as XAUTUSDT --tf 1m --price-step 0.1 \
  --venue futures --rest --flask "http://localhost:${FLASK_PORT:-5000}" > logs/binance_xaut.log 2>&1 &
PIDS+=($!)

echo "[combined-v1] starting emitter watchdog (15m hvn_inside_touch) for ${ACCOUNT}/${SYMBOL}..."
bash scripts/emitter_watchdog.sh "$FLASK_URL" "$ACCOUNT" "$SYMBOL" 30 >> logs/emitter_watchdog.log 2>&1 &
PIDS+=($!)

cat <<EOF

[combined-v1] running. Ctrl-C to stop.
  Flask:     logs/flask.log
  XAUT feed: logs/binance_xaut.log
  Emitter:   logs/exec_emit_run.log   (watchdog: logs/emitter_watchdog.log)
  Checks:    curl -s ${FLASK_URL}/health
             curl -s ${FLASK_URL}/exec/cycle_status
EOF
wait
