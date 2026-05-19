"""POST /replay — batch ingress for replay-mode sessions.

Accepts {bars: [<payload>, ...]} and stores each through the ingest pipeline.
Does NOT call Claude. Use /decide after to invoke decisions on a stored history.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from pipeline.normalizer import normalize
from pipeline.state_store import store

bp = Blueprint("replay", __name__)


@bp.post("/replay")
def replay():
    body = request.get_json(force=True, silent=True) or {}
    bars_in = body.get("bars", [])
    out = []
    for raw in bars_in:
        try:
            bar = normalize(raw)
        except Exception as e:
            out.append({"ok": False, "error": str(e)})
            continue
        if not store().put(bar):
            out.append({"ok": True, "duplicate": True, "bar_id": bar.bar_id})
            continue
        out.append({"ok": True, "bar_id": bar.bar_id})
    return jsonify({"ok": True, "results": out, "stored": sum(1 for r in out if r.get("ok") and not r.get("duplicate"))})
