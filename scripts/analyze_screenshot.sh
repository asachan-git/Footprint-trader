#!/usr/bin/env bash
# Take a screenshot of GoCharting footprint chart and send to Flask /analyze.
# Prints Claude's decision as JSON.
#
# Usage:
#   bash scripts/analyze_screenshot.sh              # interactive crosshair
#   bash scripts/analyze_screenshot.sh path/to.png  # use existing image

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLASK="http://localhost:5000"
TMP="$ROOT/data/raw/last_screenshot.png"
mkdir -p "$(dirname "$TMP")"

if [ $# -eq 1 ] && [ -f "$1" ]; then
  IMG="$1"
else
  echo "Select the footprint chart area (crosshair will appear)..."
  screencapture -i "$TMP"
  IMG="$TMP"
fi

echo "Sending $IMG to Claude..."
curl -s -X POST "$FLASK/analyze" \
  -F "image=@$IMG;type=image/png" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
if not data.get('ok'):
    print('ERROR:', data.get('error'))
    sys.exit(1)
d = data['decision']
print(f\"Side:       {d['side']}\")
print(f\"Entry:      {d.get('entry')}\")
print(f\"Stop loss:  {d.get('stop_loss')}\")
print(f\"Take profit:{d.get('take_profit')}\")
print(f\"Confidence: {d['confidence']}\")
print(f\"Rationale:  {d['rationale']}\")
if data.get('validator_reason'):
    print(f\"REJECTED:   {data['validator_reason']}\")
"
