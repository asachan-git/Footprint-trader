#!/usr/bin/env bash
# Restart JUST the Flask server (leaves feeds + decision loops running).
# Use after changing Python server/execution code — debug is off, so the
# running process won't auto-reload. Arm state is restored from disk on startup.
#
#   bash scripts/restart_server.sh
#
# For the full stack (Flask + feeds + auto-decide + grid-tick) use scripts/start.sh.

set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 1 — stop the current Flask listener on :5000 (if any)
PID="$(lsof -ti:5000 2>/dev/null | head -1)"
if [[ -n "$PID" ]]; then
  echo "[restart] stopping Flask (pid $PID)..."
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 10); do
    lsof -nP -iTCP:5000 -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
fi
if lsof -nP -iTCP:5000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[restart] ⚠️  port 5000 still held — aborting." >&2
  exit 1
fi
echo "[restart] port 5000 freed."

# 2 — relaunch Flask in the background
echo "[restart] starting Flask..."
nohup bash scripts/run_server.sh > data/server.log 2>&1 &
echo "[restart] launched (wrapper pid $!) → logs: data/server.log"

# 3 — wait for health, report
for _ in $(seq 1 15); do
  H="$(curl -s --max-time 3 http://localhost:5000/health || true)"
  [[ -n "$H" ]] && break
  sleep 1
done
if [[ -z "${H:-}" ]]; then
  echo "[restart] ⚠️  server not responding yet — check data/server.log" >&2
  tail -15 data/server.log >&2
  exit 1
fi
echo "[restart] ✓ up. health:"
echo "$H" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ok =', d.get('ok')); print('  feeds =', d.get('subsystems',{}).get('feed_gaps',{}).get('detail'))" 2>/dev/null \
  || echo "  $H"
grep -E "arm state restored" data/server.log | tail -1 | sed 's/^/  /'
