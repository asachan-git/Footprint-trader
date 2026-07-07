#!/usr/bin/env bash
# emitter_watchdog.sh — keep auto_exec_emit.sh alive for ONE account/symbol.
#
# The emitter has died silently in live sessions (no auto-restart), leaving open
# cycles managed by the server but no new bar-close emits / refresh loop. This
# watchdog polls every INTERVAL seconds; if no emitter process is running for the
# target account, it relaunches one. The emitter's own singleton mkdir-lock
# (keyed by account+symbol) makes a redundant launch a safe no-op, so this can
# fire without risk of duplicates.
#
# Scope: emitter ONLY. It deliberately does NOT watch server.app — server
# restarts are operator-driven (code edits) and must not be auto-fought.
#
# Usage:
#   scripts/emitter_watchdog.sh [FLASK_URL] [ACCOUNT] [SYMBOL] [INTERVAL_S]
#   nohup scripts/emitter_watchdog.sh http://127.0.0.1:5000 32255364 XAUUSD.pc 30 >> logs/emitter_watchdog.log 2>&1 &
set -u

FLASK="${1:-http://127.0.0.1:5000}"
ACCOUNT="${2:?account login required}"
SYMBOL="${3:-XAUUSD.pc}"
INTERVAL="${4:-30}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMIT_LOG="$ROOT/logs/exec_emit_run.log"

# Singleton: one watchdog per account+symbol (same lock convention as the emitter).
_key=$(echo "${ACCOUNT}_${SYMBOL}" | tr -c 'A-Za-z0-9_' '_')
_LOCKDIR="${TMPDIR:-/tmp}/fb_emitter_watchdog_${_key}.lock"
if ! mkdir "$_LOCKDIR" 2>/dev/null; then
    _owner=$(cat "$_LOCKDIR/pid" 2>/dev/null || echo "?")
    if kill -0 "$_owner" 2>/dev/null; then
        echo "watchdog already running for ${ACCOUNT}/${SYMBOL} (pid $_owner) — exiting" >&2
        exit 0
    fi
    rmdir "$_LOCKDIR" 2>/dev/null; mkdir "$_LOCKDIR" 2>/dev/null || { echo "lock race — exiting" >&2; exit 0; }
fi
echo "$$" > "$_LOCKDIR/pid"
trap 'rm -f "$_LOCKDIR/pid" 2>/dev/null; rmdir "$_LOCKDIR" 2>/dev/null' EXIT INT TERM

echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] up — guarding emitter ${ACCOUNT}/${SYMBOL} every ${INTERVAL}s (pid $$)"
while true; do
    # Match the emitter parent/loops by account (its cmdline carries the account arg).
    if ! pgrep -f "auto_exec_emit.sh .*${ACCOUNT}" >/dev/null 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] emitter DOWN → relaunching ${ACCOUNT}/${SYMBOL}"
        nohup bash "$ROOT/scripts/auto_exec_emit.sh" "$FLASK" "$ACCOUNT" "$SYMBOL" >> "$EMIT_LOG" 2>&1 &
    fi
    sleep "$INTERVAL"
done
