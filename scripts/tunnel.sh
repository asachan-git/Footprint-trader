#!/usr/bin/env bash
# Start Cloudflare quick tunnel and capture URL to .tunnel_url
# Usage:
#   bash scripts/tunnel.sh           # spike server (port 5001)
#   bash scripts/tunnel.sh 5000      # production Flask (port 5000)

set -euo pipefail

PORT="${1:-5001}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL_FILE="$ROOT/.tunnel_url"

echo "[tunnel] starting cloudflared → localhost:$PORT"
echo "[tunnel] URL will be written to .tunnel_url when ready"
echo ""

# Run cloudflared, tee to log, extract URL as soon as it appears
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" == *"trycloudflare.com"* ]]; then
    url=$(echo "$line" | grep -o 'https://[^ ]*trycloudflare\.com')
    if [[ -n "$url" ]]; then
      echo "$url" > "$URL_FILE"
      echo ""
      echo "════════════════════════════════════════════"
      echo "  TUNNEL URL: $url"
      echo "  SPIKE endpoint: ${url}/spike_ingest"
      echo "  PROD endpoint:  ${url}/ingest"
      echo "  Saved to: .tunnel_url"
      echo "════════════════════════════════════════════"
      echo ""
    fi
  fi
done
