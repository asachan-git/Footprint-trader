"""POST /grid_levels — footprint-driven neutral-grid placement plan.

Called by the MQL5 EA via WebRequest on each candidate bar. Returns a concrete,
pre-priced grid plan the EA places verbatim (fulcrum, buy/sell leg prices+lots,
TP-book levels, skew), or a skip verdict on chop / no-setup.

Body (flat, WebRequest-friendly):
  symbol:        str    (default: instrument.symbol)
  tf:            str    (default: primary_tf)
  current_price: float  (required for accurate placement; falls back to last close)
  trigger_hint:  str    (optional: "imbalance"|"hvn_edge"|"anchor"|"va"|"cvd_div")
  dry_run:       bool   (default: false) — compute + log, EA ignores the plan.

Dry-run plans are appended to data/grid_levels_compare.jsonl for offline review.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import asdict
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from execution.grid_planner import plan_grid_levels
from pipeline.state_store import store

bp = Blueprint("grid_levels", __name__)
LOG = logging.getLogger(__name__)

_COMPARE_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "grid_levels_compare.jsonl"


def _log_plan(plan_dict: dict) -> None:
    try:
        _COMPARE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _COMPARE_LOG.open("a") as fh:
            fh.write(json.dumps(plan_dict) + "\n")
    except Exception as e:  # logging must never break the response
        LOG.warning(f"[grid_levels] compare-log write failed (non-fatal): {e}")


@bp.post("/grid_levels")
def grid_levels():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}

    symbol = body.get("symbol") or settings["instrument"]["symbol"]
    tf = body.get("tf") or settings["instrument"]["primary_tf"]
    trigger_hint = str(body.get("trigger_hint") or "")
    dry_run = bool(body.get("dry_run", False))

    current_price = body.get("current_price")
    if current_price in (None, 0, 0.0):
        latest = store().latest(symbol, tf)
        if latest is None:
            return jsonify({"ok": False, "verdict": "skip",
                            "skip_reason": f"no bars stored for {symbol} {tf}"}), 404
        current_price = latest.ohlc.c
    current_price = float(current_price)

    try:
        plan = plan_grid_levels(symbol, tf, current_price,
                                trigger_hint=trigger_hint, settings=settings)
    except Exception as e:
        LOG.error(f"[grid_levels] planner failed: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "verdict": "skip",
                        "skip_reason": f"planner_error:{e}"}), 500

    out = {"ok": True, **asdict(plan)}

    if dry_run:
        _log_plan({"symbol": symbol, "tf": tf, "current_price": current_price, **asdict(plan)})

    return jsonify(out)
