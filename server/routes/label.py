"""POST /label — walk forward from each non-flat decision; record outcome.

For each decision in data/decisions.jsonl that doesn't yet have an entry in
data/outcomes.jsonl: pull subsequent bars from state_store, run walk_forward
to detect SL/TP/expire, append outcome row. Idempotent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from flask import Blueprint, jsonify, request

from backtest.walk_forward import label
from pipeline.state_store import store

bp = Blueprint("label", __name__)
LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
OUTCOMES = ROOT / "data" / "outcomes.jsonl"


def _existing_outcomes() -> set[str]:
    if not OUTCOMES.exists():
        return set()
    ids: set[str] = set()
    with OUTCOMES.open() as fh:
        for line in fh:
            try:
                ids.add(json.loads(line)["decision_id"])
            except Exception:
                pass
    return ids


def _close_ts_from_bar_id(bar_id: str) -> int:
    parts = bar_id.split("|")
    try:
        return int(parts[2])  # symbol|tf|close_ts|hash
    except (ValueError, IndexError):
        return 0


@bp.post("/label")
def label_pending():
    if not DECISIONS.exists():
        return jsonify({"ok": True, "labeled": 0, "note": "no decisions file yet"})

    s = store()
    body = request.get_json(silent=True) or {}
    max_lookahead = int(body.get("max_lookahead", 30))

    done = _existing_outcomes()
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)

    labeled = 0
    skipped = 0
    pending = 0

    with DECISIONS.open() as src, OUTCOMES.open("a") as dst:
        for line in src:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            did = rec.get("decision_id")
            if not did or did in done:
                continue
            dec = rec.get("decision", {})
            if dec.get("side") == "flat" or rec.get("validator_reason"):
                skipped += 1
                continue

            symbol = rec["symbol"]
            tf = rec["tf"]
            all_bars = s.recent(symbol, tf, 100_000)
            decision_close_ts = _close_ts_from_bar_id(rec["bar_id"])
            try:
                idx = next(i for i, b in enumerate(all_bars) if b.close_ts == decision_close_ts)
            except StopIteration:
                pending += 1
                continue

            forward = all_bars[idx + 1 :]
            if len(forward) < 1:
                pending += 1
                continue

            try:
                outcome = label(
                    decision_bar_id=rec["bar_id"],
                    side=dec["side"],
                    entry=float(dec["entry"]),
                    sl=float(dec["stop_loss"]),
                    tp=float(dec["take_profit"]),
                    forward=forward,
                    max_lookahead=max_lookahead,
                )
            except Exception as e:
                LOG.warning(f"label failed for {did}: {e}")
                continue

            dst.write(json.dumps({"decision_id": did, **asdict(outcome)}) + "\n")
            done.add(did)
            labeled += 1

    return jsonify({"ok": True, "labeled": labeled, "skipped_flat": skipped, "still_pending": pending})
