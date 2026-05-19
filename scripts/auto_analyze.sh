#!/usr/bin/env bash
# Auto-screenshot loop: capture GoCharting footprint chart every bar close, POST to /analyze.
#
# Usage:
#   bash scripts/auto_analyze.sh                       # full screen, 60s interval, 5s offset
#   bash scripts/auto_analyze.sh --interval 60 --offset 5
#   bash scripts/auto_analyze.sh --region 100,200,800,600    # x,y,w,h crop
#
# Region capture: do once interactively, get coordinates by:
#   1) `screencapture -i /tmp/test.png` (drag-select area in GoCharting)
#   2) Get coords from `sips -g pixelHeight -g pixelWidth /tmp/test.png` for size
#   3) For top-left, use a screen-coords tool like Digital Color Meter or just eyeball it.
# Easier: skip --region, full screen works fine for MVP.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLASK="http://localhost:5000"
INTERVAL=60
OFFSET=5
REGION=""
IMG="$ROOT/data/raw/auto_screenshot.png"
LOG="$ROOT/logs/auto_analyze.log"
LAST_HASH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --offset)   OFFSET="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$IMG")" "$(dirname "$LOG")"

echo "[auto] interval=${INTERVAL}s offset=${OFFSET}s region=${REGION:-fullscreen}"
echo "[auto] log → $LOG"
echo "[auto] Ctrl+C to stop"

while true; do
  # Sleep to next bar close + offset
  now=$(date +%s)
  next=$(( (now / INTERVAL + 1) * INTERVAL + OFFSET ))
  sleep_s=$(( next - now ))
  echo "[auto] sleeping ${sleep_s}s until next capture..."
  sleep "$sleep_s"

  # Capture
  if [[ -n "$REGION" ]]; then
    screencapture -x -R"$REGION" "$IMG"
  else
    screencapture -x "$IMG"
  fi

  # Dedup by image hash
  hash=$(shasum -a 256 "$IMG" | awk '{print $1}')
  if [[ "$hash" == "$LAST_HASH" ]]; then
    echo "[auto] $(date +%H:%M:%S) image unchanged, skip"
    continue
  fi
  LAST_HASH="$hash"

  # Send to /analyze
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  result=$(curl -s -X POST "$FLASK/analyze" -F "image=@$IMG;type=image/png" || echo '{"ok":false,"error":"curl failed"}')

  side=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('decision',{}).get('side','?'))" 2>/dev/null || echo "?")
  conf=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('decision',{}).get('confidence','?'))" 2>/dev/null || echo "?")

  line="$ts | side=$side conf=$conf"
  echo "[auto] $line"
  echo "$line | $result" >> "$LOG"
done
