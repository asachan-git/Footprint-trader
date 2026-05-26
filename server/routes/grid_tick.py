"""POST /grid_tick — Mode 2 mechanical grid trigger.

Runs the rule-based direction engine (no Claude) and dispatches a grid plan
when bias is strong enough. Body: {"symbols": ["BTCUSDT"], "tf": "15m"}
(both optional; defaults to instrument.symbol + primary_tf).

Designed to be called at every 15m bar close (cron / start.sh loop). No
LLM cost. No bar_id dedup needed (engine is deterministic per bar).
"""

from __future__ import annotations

import logging
import traceback

from flask import Blueprint, current_app, jsonify, request

from execution.direction_engine import decide_direction
from execution.router import dispatch_grid, _build_grid_plan
from execution.position_store import position_store
from llm.schema import Decision
from pipeline.state_store import store

bp = Blueprint("grid_tick", __name__)
LOG = logging.getLogger(__name__)


@bp.post("/grid_tick")
def grid_tick():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or [settings["instrument"]["symbol"]]
    primary_tf = body.get("tf") or settings["instrument"]["primary_tf"]

    results = []
    for sym in symbols:
        try:
            latest = store().latest(sym, primary_tf)
            if latest is None:
                results.append({"symbol": sym, "skipped": "no bars"})
                continue

            # Same-direction guard early (skip engine if cycle already running)
            same_dir_open = [p for p in position_store().open_positions(sym)]
            if same_dir_open:
                results.append({
                    "symbol": sym, "skipped": "cycle already open",
                    "position_id": same_dir_open[0].position_id,
                    "side": same_dir_open[0].side,
                })
                continue

            decision = decide_direction(sym, primary_tf)
            if decision.side == "flat":
                results.append({
                    "symbol": sym, "side": "flat", "score": decision.score,
                    "votes": len(decision.votes), "note": decision.note,
                })
                continue

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
            })
            LOG.info(f"[grid_tick] {sym} {decision.side} bias={decision.bias_strength} score={decision.score:.2f}")
        except Exception as e:
            LOG.exception(f"[grid_tick] {sym} error: {e}")
            results.append({"symbol": sym, "error": str(e), "trace": traceback.format_exc().splitlines()[-3:]})

    return jsonify({"ok": True, "results": results})
