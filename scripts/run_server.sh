#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -d venv ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

export PYTHONPATH="$ROOT"
exec python -m server.app
