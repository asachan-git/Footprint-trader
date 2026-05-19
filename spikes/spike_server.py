"""Minimal Flask server for Phase 0 spike — captures one payload to disk and exits.

Run: python spike_server.py
Then trigger one bar in GoCharting. Payload lands at sample_bar_lipi.json (Path A)
or sample_bar.json (Path B), and the server logs payload size + frame schema.
"""

import json
import os
import sys
from pathlib import Path

from flask import Flask, request, jsonify

HERE = Path(__file__).parent
app = Flask(__name__)


def _save(payload: dict, name: str) -> None:
    out = HERE / name
    out.write_text(json.dumps(payload, indent=2))
    size = len(json.dumps(payload))
    print(f"[spike] saved {out.name} ({size} bytes, {len(payload)} top-level keys)")


@app.post("/spike_ingest")
def spike_ingest():
    raw = request.get_data(as_text=True)
    headers = dict(request.headers)
    print(f"[spike] raw body ({len(raw)} bytes): {raw!r}")
    print(f"[spike] content-type: {headers.get('Content-Type')!r}")

    payload = request.get_json(force=True, silent=True)
    record = {
        "raw_body": raw,
        "headers": headers,
        "parsed_json": payload,
    }
    fmt = (payload or {}).get("format", "unknown") if isinstance(payload, dict) else "unknown"
    filename = "sample_bar_lipi.json" if fmt.startswith("lipi") else "sample_bar.json"
    _save(record, filename)
    return jsonify({"ok": True, "saved": filename, "got_bytes": len(raw)})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"[spike] listening on https://localhost:{port}/spike_ingest")
    print(f"[spike] IMPORTANT: visit https://localhost:{port}/health in browser first")
    print(f"[spike] click 'Advanced' → 'Proceed' to accept self-signed cert")
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context="adhoc")
