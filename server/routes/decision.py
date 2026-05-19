"""GET /decision/<id> — look up a logged decision by id."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify

bp = Blueprint("decision", __name__)
LOG = Path(__file__).resolve().parent.parent.parent / "data" / "decisions.jsonl"


@bp.get("/decision/<decision_id>")
def get_decision(decision_id: str):
    if not LOG.exists():
        return jsonify({"ok": False, "error": "no decisions log"}), 404
    with LOG.open() as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("decision_id") == decision_id:
                return jsonify({"ok": True, "record": rec})
    return jsonify({"ok": False, "error": "not found"}), 404
