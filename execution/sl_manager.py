"""SL manager — dynamic stop-loss adjustment for open grid positions.

Rules (checked on every bar ingest):
  1. Break-even: after +1R unrealized profit → move SL to avg_entry
  2. Trail: after +2R → trail SL to low of last N bars (long) / high (short)

Called from server/routes/ingest.py on every 1m bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.types import Bar
from execution.position_store import GridPosition, position_store

if TYPE_CHECKING:
    pass

LOG = logging.getLogger(__name__)

TRAIL_BARS = 3      # trail SL to low/high of last N bars
BE_TRIGGER_R = 1.0  # move to break-even after this many R in profit
TRAIL_TRIGGER_R = 2.0  # start trailing after this many R


@dataclass(frozen=True)
class SLAdjustment:
    position_id: str
    old_sl: float
    new_sl: float
    reason: str


def _unrealized_r(position: GridPosition, current_price: float) -> float:
    risk = abs(position.avg_entry - position.stop_loss)
    if risk <= 0:
        return 0.0
    if position.side == "long":
        return (current_price - position.avg_entry) / risk
    else:
        return (position.avg_entry - current_price) / risk


def check_sl_adjustments(
    bar: Bar,
    recent_bars: list[Bar],
) -> list[SLAdjustment]:
    """Check all open positions for SL update opportunities. Returns adjustments made."""
    ps = position_store()
    open_pos = ps.open_positions(bar.symbol)
    adjustments = []

    for pos in open_pos:
        current_price = bar.ohlc.c
        ur = _unrealized_r(pos, current_price)
        old_sl = pos.stop_loss
        new_sl = old_sl

        if pos.side == "long":
            # Break-even: price moved +1R → move SL to avg_entry
            if ur >= BE_TRIGGER_R and old_sl < pos.avg_entry:
                new_sl = pos.avg_entry
                reason = f"break-even after +{ur:.1f}R"

            # Trail: price moved +2R → trail to low of last N bars
            if ur >= TRAIL_TRIGGER_R:
                trail_lows = [b.ohlc.l for b in recent_bars[-TRAIL_BARS:] if b.symbol == bar.symbol]
                if trail_lows:
                    trail_level = min(trail_lows)
                    if trail_level > new_sl:
                        new_sl = trail_level
                        reason = f"trail SL to {trail_level:.2f} (low of last {TRAIL_BARS} bars)"

        else:  # short
            if ur >= BE_TRIGGER_R and old_sl > pos.avg_entry:
                new_sl = pos.avg_entry
                reason = f"break-even after +{ur:.1f}R"

            if ur >= TRAIL_TRIGGER_R:
                trail_highs = [b.ohlc.h for b in recent_bars[-TRAIL_BARS:] if b.symbol == bar.symbol]
                if trail_highs:
                    trail_level = max(trail_highs)
                    if trail_level < new_sl:
                        new_sl = trail_level
                        reason = f"trail SL to {trail_level:.2f} (high of last {TRAIL_BARS} bars)"

        if new_sl != old_sl:
            ps.adjust_sl(pos.position_id, new_sl, reason)
            adjustments.append(SLAdjustment(pos.position_id, old_sl, new_sl, reason))
            LOG.info(f"[sl_manager] {pos.position_id} {pos.side} SL {old_sl:.2f} → {new_sl:.2f} ({reason})")

    return adjustments
