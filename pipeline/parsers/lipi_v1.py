"""Parse Lipi alert payload (format=lipi_v1) → canonical Bar.

Supports both expanded ladder ({price, vol} objects) and compressed form
(base_price + tick_size + offsets) per lipi/compression.md.
"""

from __future__ import annotations

from ..types import Bar, Level, OHLC


def _decompress(payload: dict, side: str) -> tuple[Level, ...]:
    expanded_key = f"{side}_ladder"
    offsets_key = f"{side}_offsets"

    if expanded_key in payload and payload[expanded_key]:
        return tuple(Level(price=lvl["price"], vol=lvl["vol"]) for lvl in payload[expanded_key])

    if offsets_key in payload:
        base = payload["base_price"]
        tick = payload["tick_size"]
        return tuple(
            Level(price=base + off * tick, vol=vol)
            for off, vol in payload[offsets_key]
        )

    return ()


def parse(payload: dict) -> Bar:
    ohlc = payload["ohlc"]
    return Bar(
        bar_id=payload["bar_id"],
        symbol=payload["symbol"],
        tf=payload["tf"],
        close_ts=int(payload["close_ts"]),
        source=payload.get("source", "live"),
        ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
        bid_ladder=_decompress(payload, "bid"),
        ask_ladder=_decompress(payload, "ask"),
        poc=payload.get("poc"),
        delta=payload.get("delta"),
    )
