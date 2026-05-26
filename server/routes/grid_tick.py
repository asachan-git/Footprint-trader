"""POST /grid_tick — Mode 2 mechanical grid trigger.

Body:
  symbols: list[str]  (default: instrument.symbol)
  tf:      str        (default: primary_tf)
  dry_run: bool       (default: false) — compute decision but skip dispatch.
                      Use for A/B comparison vs /decide_multi (Claude).

Dry-run results are appended to data/mode_compare.jsonl for offline review.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from execution.direction_engine import decide_direction
from execution.router import dispatch_grid, _build_grid_plan
from execution.position_store import position_store
from llm.schema import Decision
from pipeline.state_store import store

bp = Blueprint("grid_tick", __name__)
LOG = logging.getLogger(__name__)

_COMPARE_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "mode_compare.jsonl"


def _log_comparison(symbol: str, decision, bar_id: str, dry_run: bool, dispatched: dict | None) -> None:
    """Append a Mode 2 decision row for later A/B vs Mode 1 (data/decisions.jsonl)."""
    try:
        row = {
            "ts": int(time.time()),
            "symbol": symbol,
            "bar_id": bar_id,
            "mode": "mode2_rules",
            "dry_run": dry_run,
            "side": decision.side,
            "bias_strength": decision.bias_strength,
            "score": decision.score,
            "votes": [{"m": v.module, "d": v.direction, "w": round(v.strength, 2),
                       "r": v.reason} for v in decision.votes],
            "dispatched_position_id": (dispatched or {}).get("position_id") if dispatched else None,
            "dispatched_skip": (dispatched or {}).get("skipped") if dispatched else None,
        }
        _COMPARE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _COMPARE_LOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:
        LOG.warning(f"[grid_tick] compare log write failed: {e}")


@bp.post("/grid_tick")
def grid_tick():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or [settings["instrument"]["symbol"]]
    primary_tf = body.get("tf") or settings["instrument"]["primary_tf"]
    dry_run = bool(body.get("dry_run", False))

    results = []
    for sym in symbols:
        try:
            latest = store().latest(sym, primary_tf)
            if latest is None:
                results.append({"symbol": sym, "skipped": "no bars"})
                continue

            # Same-direction guard (skip dispatch path; dry_run still runs engine)
            same_dir_open = [] if dry_run else [p for p in position_store().open_positions(sym)]
            if same_dir_open:
                results.append({
                    "symbol": sym, "skipped": "cycle already open",
                    "position_id": same_dir_open[0].position_id,
                    "side": same_dir_open[0].side,
                })
                continue

            decision = decide_direction(sym, primary_tf)
            if decision.side == "flat":
                _log_comparison(sym, decision, latest.bar_id, dry_run, None)
                results.append({
                    "symbol": sym, "side": "flat", "score": decision.score,
                    "votes": len(decision.votes), "note": decision.note,
                    "dry_run": dry_run,
                })
                continue

            dispatch_result = None
            if not dry_run:
                # Synthesize a Decision for grid dispatch
                d = Decision(
                    side=decision.side,
                    entry=latest.ohlc.c,
                    stop_loss=latest.ohlc.c * (0.95 if decision.side == "long" else 1.05),
                    take_profit=latest.ohlc.c * (1.05 if decision.side == "long" else 0.95),
                    confidence=min(1.0, decision.bias_strength / 5.0),
                    rationale=f"Mode2 rule-engine score={decision.score:.2f} {decision.note}",
                    bias_strength=decision.bias_strength,
                )
                plan = _build_grid_plan(d, latest, settings)
                dispatch_result = dispatch_grid(plan, latest, settings)

            _log_comparison(sym, decision, latest.bar_id, dry_run, dispatch_result)

            votes_summary = [
                {"module": v.module, "dir": v.direction, "weight": round(v.strength, 2), "reason": v.reason}
                for v in decision.votes
            ]
            results.append({
                "symbol": sym,
                "side": decision.side,
                "bias_strength": decision.bias_strength,
                "score": decision.score,
                "votes": votes_summary,
                "dispatched": dispatch_result,
                "dry_run": dry_run,
            })
            LOG.info(f"[grid_tick{'/dry' if dry_run else ''}] {sym} {decision.side} bias={decision.bias_strength} score={decision.score:.2f}")
        except Exception as e:
            LOG.exception(f"[grid_tick] {sym} error: {e}")
            results.append({"symbol": sym, "error": str(e), "trace": traceback.format_exc().splitlines()[-3:]})

    return jsonify({"ok": True, "results": results})
