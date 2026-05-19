"""GET /footprint  — ASCII footprint of latest bar(s).
   GET /vp         — Daily + weekly volume profile for a symbol.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from pipeline.footprint import build
from pipeline.state_store import store
from pipeline.features.volume_profile import from_store as vp_from_store

bp = Blueprint("footprint_view", __name__)


def _render_bar(bar) -> list[str]:
    fp = build(bar)
    if not fp.cells:
        return [f"  {bar.bar_id}: no data"]
    lines = []
    lines.append(f"\n  ── {bar.symbol} {bar.tf} close={bar.ohlc.c:.2f} delta={fp.delta:+.2f} poc={fp.poc_price} ──")
    max_vol = max(c.total for c in fp.cells) or 1
    for cell in reversed(fp.cells):  # top = high price
        bar_len = int(cell.total / max_vol * 20)
        bar_str = "█" * bar_len
        marker = " ◀ POC" if cell.price == fp.poc_price else ""
        lines.append(f"  {cell.price:>10.2f} | bid={cell.bid_vol:>7.2f} ask={cell.ask_vol:>7.2f} | {bar_str}{marker}")
    lines.append(f"  {'':>10} | total bid={fp.total_bid:.2f}  ask={fp.total_ask:.2f}  delta={fp.delta:+.2f}")
    return lines


@bp.get("/footprint")
def footprint_view():
    symbol = request.args.get("symbol", "BTCUSDT")
    tf = request.args.get("tf", "1m")
    n = min(int(request.args.get("bars", 1)), 5)
    fmt = request.args.get("fmt", "text")

    s = store()
    bars = s.recent(symbol, tf, n)
    if not bars:
        if fmt == "json":
            return jsonify({"ok": False, "error": f"no bars for {symbol} {tf}"})
        return f"no bars stored for {symbol} {tf}\n", 404, {"Content-Type": "text/plain"}

    if fmt == "json":
        out = []
        for bar in bars:
            fp = build(bar)
            out.append({
                "bar_id": bar.bar_id,
                "close": bar.ohlc.c,
                "delta": fp.delta,
                "poc": fp.poc_price,
                "total_bid": fp.total_bid,
                "total_ask": fp.total_ask,
                "cells": [{"price": c.price, "bid": c.bid_vol, "ask": c.ask_vol, "delta": c.delta} for c in fp.cells],
            })
        return jsonify({"ok": True, "bars": out})

    lines = []
    for bar in bars:
        lines.extend(_render_bar(bar))
    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain"}


@bp.get("/vp")
def vp_view():
    """GET /vp?symbol=BTCUSDT&tf=1m&period=daily|weekly|both"""
    symbol = request.args.get("symbol", "BTCUSDT")
    tf = request.args.get("tf", "1m")
    period = request.args.get("period", "both")

    def _vp_dict(vp):
        if vp is None:
            return None
        return {
            "poc": vp.poc, "vah": vp.vah, "val": vp.val,
            "shape": vp.shape, "current_position": vp.current_position,
            "hvn_zones": vp.hvn_zones, "lvn_zones": vp.lvn_zones,
            "naked_poc": vp.naked_poc, "bar_count": vp.bar_count,
            "total_volume": vp.total_volume, "price_range": vp.price_range,
        }

    result = {}
    if period in ("daily", "both"):
        result["daily"] = _vp_dict(vp_from_store(symbol, tf, "daily"))
    if period in ("weekly", "both"):
        result["weekly"] = _vp_dict(vp_from_store(symbol, tf, "weekly"))

    return jsonify({"ok": True, "symbol": symbol, **result})
