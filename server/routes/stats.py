"""GET /stats — aggregated metrics across decisions + outcomes."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify

bp = Blueprint("stats", __name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
OUTCOMES = ROOT / "data" / "outcomes.jsonl"


def _load(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    with p.open() as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


@bp.get("/stats")
def stats():
    decisions = _load(DECISIONS)
    outcomes = _load(OUTCOMES)

    total = len(decisions)
    flats = sum(1 for d in decisions if d.get("decision", {}).get("side") == "flat")
    longs = sum(1 for d in decisions if d.get("decision", {}).get("side") == "long")
    shorts = sum(1 for d in decisions if d.get("decision", {}).get("side") == "short")
    rejected = sum(1 for d in decisions if d.get("validator_reason"))

    outcomes_by_id = {o["decision_id"]: o for o in outcomes}
    wins = losses = expires = 0
    sum_r = 0.0
    sum_mfe = 0.0
    sum_mae = 0.0
    for d in decisions:
        o = outcomes_by_id.get(d["decision_id"])
        if not o:
            continue
        sum_r += float(o.get("realized_r", 0))
        sum_mfe += float(o.get("mfe_r", 0))
        sum_mae += float(o.get("mae_r", 0))
        hit = o.get("hit")
        if hit == "tp":
            wins += 1
        elif hit == "sl":
            losses += 1
        else:
            expires += 1

    scored = wins + losses + expires
    win_rate = (wins / scored) if scored else 0.0
    expectancy = (sum_r / scored) if scored else 0.0

    return jsonify({
        "ok": True,
        "decisions": {
            "total": total,
            "long": longs,
            "short": shorts,
            "flat": flats,
            "validator_rejected": rejected,
        },
        "outcomes": {
            "scored": scored,
            "tp_hits": wins,
            "sl_hits": losses,
            "expires": expires,
            "win_rate": round(win_rate, 3),
            "expectancy_r": round(expectancy, 3),
            "sum_r": round(sum_r, 2),
            "avg_mfe_r": round(sum_mfe / scored, 3) if scored else 0.0,
            "avg_mae_r": round(sum_mae / scored, 3) if scored else 0.0,
        },
    })
