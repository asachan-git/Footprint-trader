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


def _pair_trades(path: Path, symbol: str | None, tf: str | None,
                 source: str = "live") -> list[dict]:
    """Pair open/close rows in a positions.jsonl into trade shape (entry/exit,
    SL/TP, reason, R, open-time). Used for per-strategy stores and the global
    m1/m2 grid logs alike."""
    if not path.exists():
        return []
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    for line in path.open():
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
            "entry_ts_ist": o.get("ts_ist"),
            "sl": o.get("stop_loss"), "tp": o.get("take_profit"),
            "exit_ts": c.get("ts") or c.get("closed_ts"),
            "exit_ts_ist": c.get("ts_ist"),
            "exit_price": c.get("exit_price"),
            "reason": c.get("reason"), "r": c.get("realized_r"),
            "rationale": o.get("rationale"),
            "invalidation_note": o.get("invalidation_note"),
            "confidence": o.get("confidence"), "bias_strength": o.get("bias_strength"),
            "lots": o.get("lots"), "fill_type": o.get("fill_type"),
            "entry_mode": o.get("entry_mode"), "sl_mode": o.get("sl_mode"),
            "open": pid not in closes,
            "source": o.get("source") or source,
        })
    return out


def _live_trades(name: str, symbol: str | None, tf: str | None) -> list[dict]:
    """Pair open/close rows in the per-strategy positions.jsonl into trade shape."""
    return _pair_trades(_STRAT_DIR / name / "positions.jsonl", symbol, tf, source="live")


def _position_map(path: Path) -> dict[str, dict]:
    """position_id -> latest open row (for entry/SL/TP join from cycles)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.open():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") == "open" and r.get("position_id"):
            out[r["position_id"]] = r
    return out


def _pair_cycles(cycles_path: Path, positions_path: Path,
                 symbol: str | None, tf: str | None, source: str) -> list[dict]:
    """Pair cycle open/close rows by cycle_id; join the linked position's
    entry/SL/TP (cycles log carries only position_id + realized_pnl/reason).
    A cycle = one grid recovery cycle (cycle_num, parent_cycle_id chain)."""
    if not cycles_path.exists():
        return []
    pos = _position_map(positions_path)
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    for line in cycles_path.open():
        if not line.strip():
            continue
        r = json.loads(line)
        cid = r.get("cycle_id")
        if not cid:
            continue
        if r.get("type") == "open":
            opens[cid] = r
        elif r.get("type") == "close":
            closes[cid] = r
    out = []
    for cid, o in opens.items():
        if symbol and o.get("symbol") != symbol:
            continue
        if tf and o.get("tf") != tf:
            continue
        p = pos.get(o.get("position_id"), {})
        c = closes.get(cid, {})
        out.append({
            "symbol": o.get("symbol"), "tf": o.get("tf"),
            "side": o.get("direction") or p.get("side"),
            "entry_ts": o.get("ts"), "entry_ts_ist": o.get("ts_ist"),
            "entry": p.get("entry"), "sl": p.get("stop_loss"), "tp": p.get("take_profit"),
            "exit_ts": c.get("ts"), "exit_ts_ist": c.get("ts_ist"),
            "reason": c.get("reason"), "pnl": c.get("realized_pnl"),
            "rationale": p.get("rationale"),
            "cycle_num": o.get("cycle_num"), "parent_cycle_id": o.get("parent_cycle_id"),
            "open": cid not in closes,
            "is_cycle": True, "source": source,
        })
    return out


# Global grid cycle log (m1). m2 has no separate cycle log.
_CYCLE_LOGS = {
    "m1": (_ROOT / "data" / "cycles.jsonl", _ROOT / "data" / "positions.jsonl"),
}


# Global grid-mode logs (not per-strategy stores).
_GRID_LOGS = {
    "m1": _ROOT / "data" / "positions.jsonl",      # m1_claude
    "m2": _ROOT / "data" / "positions_m2.jsonl",   # m2_rules
}


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


@bp.get("/grid/<mode>/trades")
def grid_trades(mode: str):
    """Grid-mode trade history for the chart overlay. mode = m1 | m2.
    Pairs open/close rows from the global positions log; includes open time,
    entry/SL/TP, rationale, exit + realized R. Open positions have open=true."""
    mode = mode.lower()
    path = _GRID_LOGS.get(mode)
    if path is None:
        return jsonify({"ok": False, "error": f"unknown grid mode {mode!r}"}), 404
    symbol = request.args.get("symbol")
    tf = request.args.get("tf")
    trades = _pair_trades(path, symbol, tf, source=mode)
    trades.sort(key=lambda t: t.get("entry_ts") or 0)
    return jsonify({"ok": True, "mode": mode, "trades": trades})


@bp.get("/strategies/<name>/cycles")
def strategy_cycles(name: str):
    """Per-strategy cycle history (grid recovery cycles), joined with position
    entry/SL/TP. cycle_num/parent_cycle_id expose the recovery chain."""
    symbol = request.args.get("symbol")
    tf = request.args.get("tf")
    d = _STRAT_DIR / name
    cycles = _pair_cycles(d / "cycles.jsonl", d / "positions.jsonl", symbol, tf, source="live")
    cycles.sort(key=lambda t: t.get("entry_ts") or 0)
    return jsonify({"ok": True, "name": name, "cycles": cycles})


@bp.get("/grid/<mode>/cycles")
def grid_cycles(mode: str):
    """Grid-mode cycle history (m1). Pairs the global cycle log with positions."""
    mode = mode.lower()
    pair = _CYCLE_LOGS.get(mode)
    if pair is None:
        return jsonify({"ok": True, "mode": mode, "cycles": []})
    symbol = request.args.get("symbol")
    tf = request.args.get("tf")
    cycles = _pair_cycles(pair[0], pair[1], symbol, tf, source=mode)
    cycles.sort(key=lambda t: t.get("entry_ts") or 0)
    return jsonify({"ok": True, "mode": mode, "cycles": cycles})


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
