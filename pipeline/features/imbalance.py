"""Per-level diagonal imbalance.

Footprint diagonal imbalance: ask at price P vs bid at price P-1 tick.
Threshold ratio defaults to 3.0 (industry-standard).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..footprint import FootprintMatrix


@dataclass(frozen=True)
class Imbalance:
    price: float
    side: str          # "buy" | "sell"
    ratio: float


def imbalance_per_level(fp: FootprintMatrix, ratio: float = 3.0) -> tuple[Imbalance, ...]:
    out: list[Imbalance] = []
    cells = fp.cells
    for i in range(1, len(cells)):
        up_ask = cells[i].ask_vol
        down_bid = cells[i - 1].bid_vol
        if down_bid > 0 and up_ask / down_bid >= ratio:
            out.append(Imbalance(price=cells[i].price, side="buy", ratio=up_ask / down_bid))
        if up_ask > 0 and down_bid / max(up_ask, 1) >= ratio:
            out.append(Imbalance(price=cells[i - 1].price, side="sell", ratio=down_bid / max(up_ask, 1)))
    return tuple(out)
