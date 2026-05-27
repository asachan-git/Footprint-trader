"""Parse Dhan DOM footprint bar payload (format=dhan_dom_v1) → canonical Bar.

dhan_dom_v1 bars come from dhan/dom_builder.py. Structure identical to bybit_v1
(bid_ladder/ask_ladder/delta) but derived from Full packet LTQ + DOM absorption
rather than individual trades. Delta is real (buy vol - sell vol per bar).
"""

from __future__ import annotations

from ..types import Bar, Level, OHLC


def parse(payload: dict) -> Bar:
    ohlc = payload["ohlc"]
    bid = tuple(
        Level(price=float(l["price"]), vol=float(l["vol"]))
        for l in payload.get("bid_ladder", [])
    )
    ask = tuple(
        Level(price=float(l["price"]), vol=float(l["vol"]))
        for l in payload.get("ask_ladder", [])
    )
    return Bar(
        bar_id=payload["bar_id"],
        symbol=payload["symbol"],
        tf=payload["tf"],
        close_ts=int(payload["close_ts"]),
        source=payload.get("source", "live"),
        ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
        bid_ladder=bid,
        ask_ladder=ask,
        poc=payload.get("poc"),
        delta=float(payload.get("delta") or 0.0),
    )
