"""Stacked imbalance: N or more consecutive imbalances in the same direction.

A signal of strong absorption / aggression at a price zone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .imbalance import Imbalance, imbalance_per_level
from ..footprint import FootprintMatrix


@dataclass(frozen=True)
class StackedZone:
    side: str
    price_low: float
    price_high: float
    count: int


def stacked_imbalances(
    fp: FootprintMatrix,
    min_stack: int = 3,
    ratio: float = 3.0,
) -> tuple[StackedZone, ...]:
    imbs = imbalance_per_level(fp, ratio=ratio)
    if not imbs:
        return ()
    out: list[StackedZone] = []
    run: list[Imbalance] = []
    for imb in imbs:
        if run and imb.side == run[-1].side:
            run.append(imb)
        else:
            if len(run) >= min_stack:
                out.append(StackedZone(
                    side=run[0].side,
                    price_low=min(i.price for i in run),
                    price_high=max(i.price for i in run),
                    count=len(run),
                ))
            run = [imb]
    if len(run) >= min_stack:
        out.append(StackedZone(
            side=run[0].side,
            price_low=min(i.price for i in run),
            price_high=max(i.price for i in run),
            count=len(run),
        ))
    return tuple(out)
