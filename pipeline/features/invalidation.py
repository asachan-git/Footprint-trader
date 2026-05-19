"""Footprint invalidation detection.

A position is invalidated (exit immediately) when:
1. Opposite-side absorption forms AT or INSIDE the entry zone
2. Price closes on wrong side of entry with strong delta confirming reversal

Separate from SL price hit — invalidation can trigger before SL is touched.
This is the key improvement over the old ATR grid: we exit when the footprint
TELLS us we're wrong, not when price reaches an arbitrary SL level.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..footprint import FootprintMatrix
from ..types import Bar
from .absorption import detect_absorption

ENTRY_ZONE_PCT = 0.0005   # price within 0.05% of entry = "at entry zone" (was 0.2% — too wide)
DELTA_THRESHOLD = 0.0     # any negative close delta confirms reversal


@dataclass(frozen=True)
class InvalidationSignal:
    reason: str
    price: float
    strength: str          # "strong" | "moderate"


def detect_invalidation(
    bar: Bar,
    fp: FootprintMatrix,
    side: str,
    entry: float,
    delta_threshold: float = DELTA_THRESHOLD,
    entry_zone_pct: float = ENTRY_ZONE_PCT,
) -> InvalidationSignal | None:
    """Return InvalidationSignal if bar invalidates the position, else None.

    side: "long" | "short"
    entry: original entry price of position
    """
    absorptions = detect_absorption(bar, fp, absorb_ratio=0.20)
    entry_zone = entry * entry_zone_pct

    if side == "long":
        # Structural gate: bar must have tested BELOW entry before checking absorption.
        # Sell absorption ABOVE entry is normal resistance — it does NOT invalidate a long.
        # Only when price has dipped below entry do we check if sellers confirmed it.
        if bar.ohlc.l < entry:
            for abs_zone in absorptions:
                if abs_zone.side == "sell" and abs_zone.price <= entry + entry_zone:
                    return InvalidationSignal(
                        reason=(
                            f"sell absorption {abs_zone.bar_pct:.0%} at {abs_zone.price:.2f} "
                            f"while bar_low={bar.ohlc.l:.2f} < entry {entry:.2f}"
                        ),
                        price=abs_zone.price,
                        strength="strong",
                    )
        # Moderate: close below entry with meaningful negative delta
        if bar.ohlc.c < entry and fp.delta < delta_threshold:
            return InvalidationSignal(
                reason=f"bar closed {bar.ohlc.c:.2f} < entry {entry:.2f} with delta {fp.delta:.2f}",
                price=bar.ohlc.c,
                strength="moderate",
            )

    elif side == "short":
        # Structural gate: bar must have tested ABOVE entry first.
        # Buy absorption BELOW entry is normal support — does NOT invalidate a short.
        if bar.ohlc.h > entry:
            for abs_zone in absorptions:
                if abs_zone.side == "buy" and abs_zone.price >= entry - entry_zone:
                    return InvalidationSignal(
                        reason=(
                            f"buy absorption {abs_zone.bar_pct:.0%} at {abs_zone.price:.2f} "
                            f"while bar_high={bar.ohlc.h:.2f} > entry {entry:.2f}"
                        ),
                        price=abs_zone.price,
                        strength="strong",
                    )
        # Moderate: close above entry with positive delta
        if bar.ohlc.c > entry and fp.delta > -delta_threshold:
            return InvalidationSignal(
                reason=f"bar closed {bar.ohlc.c:.2f} > entry {entry:.2f} with delta {fp.delta:.2f}",
                price=bar.ohlc.c,
                strength="moderate",
            )

    return None


def check_tp_absorption(
    bar: Bar,
    fp: FootprintMatrix,
    side: str,
    take_profit: float,
    tp_zone_pct: float = 0.003,
    entry: float | None = None,
) -> str | None:
    """Return reason string if bar shows opposite absorption near TP, else None.

    This is the full-exit signal: close entire position when opposite side
    absorbs at the TP zone (they've taken profit, reversal likely).
    """
    # Gate: price must have reached at least 95% of the way to TP
    if entry is not None and entry != take_profit:
        progress = (
            (bar.ohlc.h - entry) / (take_profit - entry)
            if side == "long"
            else (entry - bar.ohlc.l) / (entry - take_profit)
        )
        if progress < 0.95:
            return None
    absorptions = detect_absorption(bar, fp, absorb_ratio=0.20)
    tp_zone = take_profit * tp_zone_pct

    for abs_zone in absorptions:
        if side == "long" and abs_zone.side == "sell":
            if abs(abs_zone.price - take_profit) <= tp_zone:
                return f"sell absorption {abs_zone.bar_pct:.0%} at {abs_zone.price:.2f} near TP {take_profit:.2f}"
        elif side == "short" and abs_zone.side == "buy":
            if abs(abs_zone.price - take_profit) <= tp_zone:
                return f"buy absorption {abs_zone.bar_pct:.0%} at {abs_zone.price:.2f} near TP {take_profit:.2f}"
    return None
