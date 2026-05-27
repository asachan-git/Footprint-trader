"""Parse Dhan LTP-based bar payload (format=dhan_v1) → canonical Bar.

dhan_v1 bars come from dhan/bar_builder.py. Key differences from bybit_v1:
- bid/ask keys (not bid_ladder/ask_ladder) hold volume-spread-evenly ladders
- delta is always 0.0 (no trade-level aggressor data from Dhan API)
"""

from __future__ import annotations

from ..types import Bar, Level, OHLC


def parse(payload: dict) -> Bar:
    ohlc = payload["ohlc"]
    bid = [Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("bid", [])]
    ask = [Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("ask", [])]
    return Bar(
        bar_id=payload["bar_id"],
        symbol=payload["symbol"],
        tf=payload["tf"],
        close_ts=int(payload["close_ts"]),
        source=payload.get("source", "live"),
        ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
        bid_ladder=tuple(bid),
        ask_ladder=tuple(ask),
        poc=payload.get("poc"),
        delta=float(payload.get("delta") or 0.0),
    )
