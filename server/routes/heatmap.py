"""GET /heatmap — trade-volume heatmap data for browser-based visualization.

Returns per-bar, per-price-level volume data for rendering a Bookmap-style
heatmap in the browser. Color = delta (green=net buying, red=net selling),
brightness = total volume. Overlays: POC line, VAH/VAL bands, HVN zones,
big_trade absorption markers.

Usage:
  GET /heatmap?symbol=BTCUSDT&minutes=60&price_step=10
  GET /heatmap?symbol=XAUTUSDT&minutes=120&price_step=0.1

Browser UI:
  GET /heatmap.html  → full canvas visualization
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from pipeline.state_store import store
from pipeline.footprint import build as build_fp
import pipeline.features.vp_cache as vp_cache

bp = Blueprint("heatmap", __name__)

ROOT = Path(__file__).resolve().parent.parent.parent


@bp.get("/heatmap")
def heatmap_data():
    settings = current_app.config["FB_SETTINGS"]
    symbol    = request.args.get("symbol") or settings["instrument"]["symbol"]
    minutes   = int(request.args.get("minutes", 60))
    price_step = float(request.args.get(
        "price_step",
        10.0 if "BTC" in symbol else 0.1
    ))
    minutes = max(5, min(minutes, 480))

    s = store()
    primary_tf = settings["instrument"]["primary_tf"]
    bars = s.recent(symbol, primary_tf, minutes + 10)
    if not bars:
        return jsonify({"ok": False, "error": f"no bars for {symbol}"}), 404

    # Limit to requested time window
    cutoff_ts = bars[-1].close_ts - minutes * 60
    bars = [b for b in bars if b.close_ts >= cutoff_ts]

    # Get VP context
    daily_vp  = vp_cache.get(symbol, "daily")
    prior_vp  = _prior_day_vp(symbol)

    # Load big_trade events for marker overlay
    big_trades = _load_big_trades(symbol, cutoff_ts)

    # Build heatmap bars
    heatmap_bars = []
    for bar in bars:
        fp = build_fp(bar)
        cells = _build_cells(bar, fp, price_step)
        bar_trades = [bt for bt in big_trades
                      if abs(bt["ts"] - bar.close_ts) < 60]
        heatmap_bars.append({
            "ts":        bar.close_ts,
            "poc":       fp.poc_price,
            "delta":     round(fp.delta, 4),
            "cells":     cells,
            "markers":   bar_trades,
        })

    return jsonify({
        "ok":        True,
        "symbol":    symbol,
        "minutes":   minutes,
        "price_step": price_step,
        "bars":      heatmap_bars,
        "daily_vp":  daily_vp,
        "prior_vp":  prior_vp,
    })


@bp.get("/heatmap.html")
def heatmap_ui():
    html_path = ROOT / "static" / "heatmap.html"
    if html_path.exists():
        return send_file(html_path)
    return _inline_heatmap_html(), 200, {"Content-Type": "text/html"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _round_price(price: float, step: float) -> float:
    return round(round(price / step) * step, 8)


def _build_cells(bar, fp, price_step: float) -> list[dict]:
    """Aggregate bid/ask volume per price bucket from the footprint."""
    buckets: dict[float, dict] = {}

    def add(price, bid, ask):
        key = _round_price(price, price_step)
        if key not in buckets:
            buckets[key] = {"price": key, "bid": 0.0, "ask": 0.0}
        buckets[key]["bid"] += bid
        buckets[key]["ask"] += ask

    for lvl in bar.bid_ladder:
        add(lvl.price, lvl.vol, 0.0)
    for lvl in bar.ask_ladder:
        add(lvl.price, 0.0, lvl.vol)

    # Get VP zones for this bar's price range
    daily_vp = vp_cache.get(bar.symbol, "daily") or {}
    poc   = daily_vp.get("poc")
    hvns  = {z["low"]: z["high"] for z in daily_vp.get("hvn_zones", [])}
    lvns  = {z["low"]: z["high"] for z in daily_vp.get("lvn_zones", [])}
    vah   = daily_vp.get("vah")
    val   = daily_vp.get("val")

    cells = []
    for key, b in sorted(buckets.items()):
        total = b["bid"] + b["ask"]
        delta = b["ask"] - b["bid"]
        is_poc = poc is not None and abs(key - poc) < price_step
        is_hvn = any(lo <= key <= hi for lo, hi in hvns.items())
        is_lvn = any(lo <= key <= hi for lo, hi in lvns.items())
        at_vah = vah is not None and abs(key - vah) < price_step
        at_val = val is not None and abs(key - val) < price_step
        cells.append({
            "price":  key,
            "bid":    round(b["bid"], 4),
            "ask":    round(b["ask"], 4),
            "total":  round(total, 4),
            "delta":  round(delta, 4),
            "poc":    is_poc,
            "hvn":    is_hvn,
            "lvn":    is_lvn,
            "vah":    at_vah,
            "val":    at_val,
        })
    return cells


def _prior_day_vp(symbol: str) -> dict | None:
    hist = vp_cache.get_history(symbol, "daily", n=2)
    if len(hist) >= 2:
        pd = hist[-2]
        return {k: pd.get(k) for k in ["poc", "vah", "val", "shape", "hvn_zones", "lvn_zones"]}
    return None


def _load_big_trades(symbol: str, since_ts: int) -> list[dict]:
    bt_file = ROOT / "data" / "big_trades.jsonl"
    if not bt_file.exists():
        return []
    events = []
    try:
        for line in bt_file.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("symbol") == symbol and e.get("ts", 0) >= since_ts:
                events.append({
                    "ts":       e["ts"],
                    "price":    e["price"],
                    "side":     e["aggressor"],
                    "outcome":  e.get("outcome", "pending"),
                    "vol":      e.get("volume", 0),
                })
    except Exception:
        pass
    return events


def _inline_heatmap_html() -> str:
    """Fallback inline HTML if static/heatmap.html not found."""
    return "<html><body><p>static/heatmap.html not found. Run server and check static/.</p></body></html>"
