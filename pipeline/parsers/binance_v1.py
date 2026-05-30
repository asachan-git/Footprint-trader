"""Parse Binance Futures aggTrade footprint payload (format=binance_v1) → canonical Bar."""

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
        bid_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("bid_ladder", [])),
        ask_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("ask_ladder", [])),
        poc=payload.get("poc"),
        delta=float(payload["delta"]) if payload.get("delta") is not None else None,
        cvd_open=float(payload["cvd_open"]) if payload.get("cvd_open") is not None else None,
        cvd_high=float(payload["cvd_high"]) if payload.get("cvd_high") is not None else None,
        cvd_low=float(payload["cvd_low"]) if payload.get("cvd_low") is not None else None,
        cvd_close=float(payload["cvd_close"]) if payload.get("cvd_close") is not None else None,
    )
