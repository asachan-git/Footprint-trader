"""Strategy manager routes.

GET  /strategies                  → list deployed strategies + headline results
GET  /strategies/<name>/results   → full per-strategy stats + equity curve
POST /strategies/tick             → run one manager tick (manage + maybe enter)
                                     body: {symbols?: [...], tf?: str}

The manager is a process-global singleton so its per-strategy stores (and the
last-cum-R equity tracking) persist across requests.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from pipeline.state_store import store
from strategies.manager import StrategyManager, get_manager

bp = Blueprint("strategies", __name__)
LOG = logging.getLogger(__name__)


def manager() -> StrategyManager:
    return get_manager(current_app.config.get("FB_SETTINGS"))


@bp.get("/strategies")
def list_strategies():
    mgr = manager()
    out = []
    for s in mgr.strategies:
        res = mgr.results(s.name)
        out.append({
            "name": s.name,
            "symbols": s.symbols(current_app.config["FB_SETTINGS"]),
            "overall": res.get("overall"),
            "equity": res.get("equity"),
        })
    return jsonify({"ok": True, "strategies": out})


@bp.get("/strategies/<name>/results")
def strategy_results(name: str):
    mgr = manager()
    if name not in {s.name for s in mgr.strategies}:
        return jsonify({"ok": False, "error": f"unknown strategy {name!r}"}), 404
    return jsonify({"ok": True, **mgr.results(name)})


@bp.post("/strategies/tick")
def tick():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or (settings.get("vp_cache") or {}).get(
        "symbols", [settings["instrument"]["symbol"]])
    tf = body.get("tf") or settings["instrument"]["primary_tf"]
    mgr = manager()

    out = []
    for sym in symbols:
        latest = store().latest(sym, tf)
        if latest is None:
            out.append({"symbol": sym, "skipped": "no bars"})
            continue
        for r in mgr.tick(sym, tf, latest, settings):
            out.append({"symbol": sym, "strategy": r.strategy,
                        "action": r.action, **r.detail})
    return jsonify({"ok": True, "results": out})
