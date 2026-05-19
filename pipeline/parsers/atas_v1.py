"""Parse ATAS indicator payload (format=atas_v1) → canonical Bar.

ATAS provides full per-price footprint via PriceVolumeInfo.Bid / .Ask.
bid_ladder and ask_ladder are lists of {price, vol}.
"""

from __future__ import annotations

from ..types import Bar, Level, OHLC


def parse(payload: dict) -> Bar:
    ohlc = payload["ohlc"]
    return Bar(
        bar_id=payload["bar_id"],
        symbol=payload["symbol"],
        tf=payload["tf"],
        close_ts=int(payload["close_ts"]),
        source=payload.get("source", "live"),
        ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
        bid_ladder=tuple(Level(price=float(lvl["price"]), vol=float(lvl["vol"])) for lvl in payload.get("bid_ladder", [])),
        ask_ladder=tuple(Level(price=float(lvl["price"]), vol=float(lvl["vol"])) for lvl in payload.get("ask_ladder", [])),
        poc=payload.get("poc"),
        delta=float(payload["delta"]) if payload.get("delta") is not None else None,
    )
