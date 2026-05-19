"""Footprint matrix built from a canonical Bar's bid/ask ladders.

A footprint is the per-price-level bid vs ask traded volume for a single bar.
This module provides:
- `FootprintMatrix`: sorted price → (bid_vol, ask_vol) view
- Derived scalars: total bid, total ask, delta, POC, value area
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .types import Bar, Level


@dataclass(frozen=True)
class Cell:
    price: float
    bid_vol: float
    ask_vol: float

    @property
    def total(self) -> float:
        return self.bid_vol + self.ask_vol

    @property
    def delta(self) -> float:
        return self.ask_vol - self.bid_vol


@dataclass(frozen=True)
class FootprintMatrix:
    cells: tuple[Cell, ...]   # sorted by price ascending

    @property
    def total_bid(self) -> float:
        return sum(c.bid_vol for c in self.cells)

    @property
    def total_ask(self) -> float:
        return sum(c.ask_vol for c in self.cells)

    @property
    def delta(self) -> float:
        return self.total_ask - self.total_bid

    @property
    def poc_price(self) -> float | None:
        if not self.cells:
            return None
        return max(self.cells, key=lambda c: c.total).price

    def value_area(self, pct: float = 0.70) -> tuple[float, float] | None:
        """Inclusive [low, high] price range covering `pct` of total volume around POC."""
        if not self.cells:
            return None
        total = sum(c.total for c in self.cells)
        if total == 0:
            return None
        target = total * pct
        # walk outward from POC index
        ordered = sorted(self.cells, key=lambda c: c.price)
        prices = [c.price for c in ordered]
        vols = [c.total for c in ordered]
        poc_idx = max(range(len(ordered)), key=lambda i: vols[i])
        lo = hi = poc_idx
        acc = vols[poc_idx]
        while acc < target and (lo > 0 or hi < len(ordered) - 1):
            left = vols[lo - 1] if lo > 0 else -1
            right = vols[hi + 1] if hi < len(ordered) - 1 else -1
            if right >= left and hi < len(ordered) - 1:
                hi += 1
                acc += vols[hi]
            elif lo > 0:
                lo -= 1
                acc += vols[lo]
            else:
                break
        return prices[lo], prices[hi]


def _merge_ladders(bid: Iterable[Level], ask: Iterable[Level]) -> tuple[Cell, ...]:
    by_price: dict[float, list[float]] = {}
    for lvl in bid:
        by_price.setdefault(lvl.price, [0.0, 0.0])[0] += lvl.vol
    for lvl in ask:
        by_price.setdefault(lvl.price, [0.0, 0.0])[1] += lvl.vol
    return tuple(
        Cell(price=p, bid_vol=b, ask_vol=a)
        for p, (b, a) in sorted(by_price.items())
    )


def build(bar: Bar) -> FootprintMatrix:
    return FootprintMatrix(cells=_merge_ladders(bar.bid_ladder, bar.ask_ladder))
