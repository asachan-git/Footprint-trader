"""Grid manager — controls multi-leg footprint grid position logic.

Decides whether to add a leg to an existing position based on footprint confirmation.
Prevents the old martingale failure: NEVER adds leg if footprint shows opposing pressure.

Rules (configurable in config/settings.yaml::grid):
  max_legs: 3
  leg_add_min_delta: positive for long, negative for short
  leg_add_require_absorption: bid absorption at entry zone for longs
  entry_zone_tolerance: 0.003 (price within 0.3% of entry)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pipeline.footprint import FootprintMatrix
from pipeline.types import Bar
from pipeline.features.absorption import detect_absorption
from pipeline.features.stacked_imbalance import stacked_imbalances
from execution.position_store import GridPosition

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegAddSignal:
    should_add: bool
    reason: str
    suggested_entry: float | None = None


def should_add_leg(
    bar: Bar,
    fp: FootprintMatrix,
    position: GridPosition,
    max_legs: int = 3,
    entry_zone_pct: float = 0.003,
    min_delta_ratio: float = 0.0,
) -> LegAddSignal:
    """Determine if a new leg should be added to an existing grid position.

    Conditions to ADD (all must pass):
    1. Leg count < max_legs
    2. Entry zone not broken (price hasn't closed beyond stop_loss)
    3. Footprint confirms direction: absorption + delta confirm same side
    4. No invalidation signal present

    Returns LegAddSignal(should_add=False, ...) to PREVENT adding on pure price decline.
    """
    if position.leg_count >= max_legs:
        return LegAddSignal(False, f"max legs reached ({max_legs})")

    # Gate 1: entry zone must still hold (price above SL for long)
    if position.side == "long" and bar.ohlc.l <= position.stop_loss:
        return LegAddSignal(False, f"SL breached: bar low {bar.ohlc.l} ≤ SL {position.stop_loss}")
    if position.side == "short" and bar.ohlc.h >= position.stop_loss:
        return LegAddSignal(False, f"SL breached: bar high {bar.ohlc.h} ≥ SL {position.stop_loss}")

    entry_zone = position.avg_entry * entry_zone_pct
    absorptions = detect_absorption(bar, fp, absorb_ratio=0.20)
    stacked = stacked_imbalances(fp, min_stack=2)

    if position.side == "long":
        # Need positive delta or bid absorption at/near entry zone
        delta_ok = fp.delta >= min_delta_ratio * fp.total_bid if fp.total_bid > 0 else fp.delta >= 0
        absorb_ok = any(
            a.side == "buy" and abs(a.price - position.avg_entry) <= entry_zone
            for a in absorptions
        )
        imbalance_ok = any(s.side == "buy" for s in stacked)

        if not delta_ok:
            return LegAddSignal(False, f"delta {fp.delta:.2f} not confirming long")
        if not (absorb_ok or imbalance_ok):
            return LegAddSignal(False, "no bid absorption or stacked buy imbalance at entry zone")

        # Reject if opposite absorption present at entry
        sell_absorb_at_entry = any(
            a.side == "sell" and abs(a.price - position.avg_entry) <= entry_zone
            for a in absorptions
        )
        if sell_absorb_at_entry:
            return LegAddSignal(False, "sell absorption at entry zone — opposing pressure")

        suggested = min(bar.ohlc.l, position.avg_entry) - (position.avg_entry - position.stop_loss) * 0.3
        return LegAddSignal(True, f"leg {position.leg_count + 1}: delta {fp.delta:.2f} + buy confirmation", suggested)

    else:  # short
        delta_ok = fp.delta <= -min_delta_ratio * fp.total_ask if fp.total_ask > 0 else fp.delta <= 0
        absorb_ok = any(
            a.side == "sell" and abs(a.price - position.avg_entry) <= entry_zone
            for a in absorptions
        )
        imbalance_ok = any(s.side == "sell" for s in stacked)

        if not delta_ok:
            return LegAddSignal(False, f"delta {fp.delta:.2f} not confirming short")
        if not (absorb_ok or imbalance_ok):
            return LegAddSignal(False, "no ask absorption or stacked sell imbalance at entry zone")

        buy_absorb_at_entry = any(
            a.side == "buy" and abs(a.price - position.avg_entry) <= entry_zone
            for a in absorptions
        )
        if buy_absorb_at_entry:
            return LegAddSignal(False, "buy absorption at entry zone — opposing pressure")

        suggested = max(bar.ohlc.h, position.avg_entry) + (position.stop_loss - position.avg_entry) * 0.3
        return LegAddSignal(True, f"leg {position.leg_count + 1}: delta {fp.delta:.2f} + sell confirmation", suggested)


def active_grid_summary(position: GridPosition) -> dict:
    """Compact grid context for Claude prompt — tells Claude what's already open."""
    return {
        "position_id": position.position_id,
        "side": position.side,
        "leg_count": position.leg_count,
        "avg_entry": round(position.avg_entry, 2),
        "stop_loss": round(position.stop_loss, 2),
        "take_profit": round(position.take_profit, 2),
        "legs": [
            {"leg": l.leg, "entry": l.entry, "confidence": l.confidence}
            for l in position.legs
        ],
    }
