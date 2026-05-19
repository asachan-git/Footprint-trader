"""Absorption: high volume at a price level with little/no price progression past it.

Heuristic: a cell at high/low of bar with >= absorb_ratio of bar volume, and bar
closes within `wick_tolerance` ticks of that level.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..footprint import FootprintMatrix
from ..types import Bar


@dataclass(frozen=True)
class Absorption:
    price: float
    side: str        # "buy" (absorbed at low) | "sell" (absorbed at high)
    volume: int
    bar_pct: float


def detect_absorption(
    bar: Bar,
    fp: FootprintMatrix,
    absorb_ratio: float = 0.30,
    wick_tolerance: float = 0.0,
) -> tuple[Absorption, ...]:
    if not fp.cells:
        return ()
    total = sum(c.total for c in fp.cells)
    if total == 0:
        return ()
    out: list[Absorption] = []
    high = bar.ohlc.h
    low = bar.ohlc.l
    for c in fp.cells:
        pct = c.total / total
        if pct < absorb_ratio:
            continue
        if abs(c.price - low) <= wick_tolerance:
            out.append(Absorption(price=c.price, side="buy", volume=c.total, bar_pct=pct))
        elif abs(c.price - high) <= wick_tolerance:
            out.append(Absorption(price=c.price, side="sell", volume=c.total, bar_pct=pct))
    return tuple(out)
