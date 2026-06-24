#!/usr/bin/env bash
# mt5_server.sh — run the direct-MT5 rpyc server UNDER wine, alongside terminal64.exe.
#
# The MetaTrader5 python module is Windows-only, so it can only run in the Windows
# python we installed inside the MetaQuotes wine prefix. This script drives that
# wine-python on execution/mt5_bridge/server.py, which exposes a localhost rpyc
# service the mac venv connects to (execution/mt5_direct.py).
#
# Prereqs (one-time, already done):
#   - C:\python312\python.exe in the prefix (64-bit embeddable)
#   - pip install MetaTrader5 numpy==1.26.4 rpyc  (numpy 2.x hits a wine ucrtbase gap)
#   - terminal64.exe RUNNING + logged in + AutoTrading enabled
#
# Usage: scripts/mt5_server.sh [port]
set -u

PORT="${1:-18812}"
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
export WINEPREFIX="/Users/aniteksachan/Library/Application Support/net.metaquotes.wine.metatrader5"
export WINEDEBUG=-all

# repo server.py reached through wine's Z: drive (Z: -> /)
SRV='Z:\Users\aniteksachan\Strategies\FootprintBiot\execution\mt5_bridge\server.py'

echo "[mt5_server] launching wine-python rpyc server on 127.0.0.1:${PORT}"
exec "$WINE" 'C:\python312\python.exe' "$SRV" "$PORT"
