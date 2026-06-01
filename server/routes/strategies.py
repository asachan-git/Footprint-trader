"""Strategy manager routes.

GET  /strategies                  → list deployed strategies + headline results
GET  /strategies/<name>/results   → full per-strategy stats + equity curve
POST /strategies/tick             → run one manager tick (manage + maybe enter)
                                     body: {symbols?: [...], tf?: str}

The manager is a process-global singleton so its per-strategy stores (and the
last-cum-R equity tracking) persist across requests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from pipeline.state_store import store
from strategies.manager import StrategyManager, get_manager

bp = Blueprint("strategies", __name__)
LOG = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_STRAT_DIR = _ROOT / "data" / "strategies"


def _backtest_trades(name: str, symbol: str | None, tf: str | None) -> list[dict]:
    f = _STRAT_DIR / name / "backtest_trades.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.open():
        if not line.strip():
            continue
        r = json.loads(line)
        if symbol and r.get("symbol") != symbol:
            continue
        if tf and r.get("tf") != tf:
            continue
        out.append(r)
    return out


def _live_trades(name: str, symbol: str | None, tf: str | None) -> list[dict]:
    """Pair open/close rows in the per-strategy positions.jsonl into trade shape."""
    f = _STRAT_DIR / name / "positions.jsonl"
    if not f.exists():
        return []
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    for line in f.open():
        if not line.strip():
            continue
        r = json.loads(line)
        pid = r.get("position_id")
        if not pid:
            continue
        if r.get("type") == "open":
            opens[pid] = r
        elif r.get("type") == "close":
            closes[pid] = r
    out = []
    for pid, o in opens.items():
        if symbol and o.get("symbol") != symbol:
            continue
        if tf and o.get("tf") != tf:
            continue
        c = closes.get(pid, {})
        out.append({
            "symbol": o.get("symbol"), "tf": o.get("tf"), "side": o.get("side"),
            "entry_ts": o.get("ts"), "entry": o.get("entry"),
            "sl": o.get("stop_loss"), "tp": o.get("take_profit"),
            "exit_ts": c.get("ts") or c.get("closed_ts"),
            "exit_price": c.get("exit_price"),
            "reason": c.get("reason"), "r": c.get("realized_r"),
            "entry_mode": o.get("entry_mode"), "sl_mode": o.get("sl_mode"),
            "source": "live",
        })
    return out


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


@bp.get("/strategies/<name>/trades")
def strategy_trades(name: str):
    """Trades for chart overlay. source = backtest | live | all (default backtest)."""
    symbol = request.args.get("symbol")
    tf = request.args.get("tf")
    source = (request.args.get("source") or "backtest").lower()
    trades: list[dict] = []
    if source in ("backtest", "all"):
        trades += _backtest_trades(name, symbol, tf)
    if source in ("live", "all"):
        trades += _live_trades(name, symbol, tf)
    trades.sort(key=lambda t: t.get("entry_ts") or 0)
    return jsonify({"ok": True, "name": name, "source": source, "trades": trades})


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
