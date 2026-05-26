"""Wick-trap detector — identifies trapped traders via footprint wick analysis.

A "trap" occurs when a bar wicks aggressively in one direction but the footprint
reveals the aggressor flow was AGAINST the wick direction:

  Bull trap (sellers trapped short):
    - Bar has significant lower wick (price swept below a level then recovered)
    - Bid side (buy aggression) in the wick zone is high relative to ask
    - OR ask vol is low in the wick (sellers couldn't push through)
    - Result: traders who shorted the sweep are trapped → expect squeeze up

  Bear trap (buyers trapped long):
    - Bar has significant upper wick
    - Ask side (sell aggression) in the wick zone is high relative to bid
    - OR bid vol is low in the wick (buyers couldn't sustain the push)
    - Result: traders who longed the breakout are trapped → expect sell-off

Used by:
  - cycle_manager: hedge invalidation additional signal
  - grid_placer: confirmation for direction bias
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar
from pipeline.footprint import FootprintMatrix


@dataclass(frozen=True)
class WickTrap:
    side: Literal["bull_trap", "bear_trap"]
    wick_pct: float       # wick size as fraction of total bar range
    wick_vol: float       # total volume in the wick zone
    body_vol: float       # total volume in body zone
    wick_bid_ratio: float # bid/(bid+ask) in wick zone; high = buyers in lower wick (bull trap)
    confidence: float     # 0..1


def detect_wick_trap(bar: Bar, fp: FootprintMatrix, min_wick_pct: float = 0.30) -> WickTrap | None:
    """Detect a trapped-trader signal on this bar.

    min_wick_pct: wick must be at least this fraction of total range to count.
    Returns None if no trap signal.
    """
    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    total_range = h - l
    if total_range <= 0:
        return None

    body_high = max(o, c)
    body_low = min(o, c)
    upper_wick = h - body_high
    lower_wick = body_low - l

    upper_wick_pct = upper_wick / total_range
    lower_wick_pct = lower_wick / total_range

    # ── Collect footprint volume in wick vs body zones ─────────────────────
    wick_bid = wick_ask = body_bid = body_ask = 0.0
    for lvl in bar.bid_ladder:
        if lvl.price <= body_low:
            wick_bid += lvl.vol
        else:
            body_bid += lvl.vol
    for lvl in bar.ask_ladder:
        if lvl.price >= body_high:
            wick_ask += lvl.vol
        else:
            body_ask += lvl.vol

    wick_vol = wick_bid + wick_ask
    body_vol = body_bid + body_ask

    # ── Lower wick trap: bearish sweep + buyers defended (bull trap for shorts) ──
    if lower_wick_pct >= min_wick_pct:
        total_wick = wick_bid + wick_ask + 1e-9
        wick_bid_ratio = wick_bid / total_wick
        # High bid ratio in lower wick = buyers stepped in to defend the sweep
        # Low close after large lower wick = bear trap (buyers got absorbed, actually bearish)
        if wick_bid_ratio >= 0.60 and c > (l + total_range * 0.5):
            # Price recovered to upper half with strong bid in wick = bull trap (shorts squeezed)
            conf = min(1.0, lower_wick_pct * wick_bid_ratio * 2.0)
            return WickTrap(
                side="bull_trap",
                wick_pct=lower_wick_pct,
                wick_vol=wick_vol,
                body_vol=body_vol,
                wick_bid_ratio=wick_bid_ratio,
                confidence=round(conf, 3),
            )
        elif wick_bid_ratio < 0.40:
            # Low bid ratio in lower wick = buyers didn't step in → bearish continuation
            return None  # not a bull trap; continuation signal handled elsewhere

    # ── Upper wick trap: bullish breakout absorbed → bear trap ──────────────
    if upper_wick_pct >= min_wick_pct:
        total_wick_upper = wick_ask + (wick_bid * 0) + 1e-9  # upper wick uses ask-side
        # Recompute for upper wick
        uw_bid = uw_ask = 0.0
        for lvl in bar.ask_ladder:
            if lvl.price >= body_high:
                uw_ask += lvl.vol
        for lvl in bar.bid_ladder:
            if lvl.price >= body_high:
                uw_bid += lvl.vol
        uw_total = uw_bid + uw_ask + 1e-9
        uw_ask_ratio = uw_ask / uw_total
        # High ask ratio in upper wick + close below body midpoint = bear trap
        if uw_ask_ratio >= 0.60 and c < (l + total_range * 0.5):
            conf = min(1.0, upper_wick_pct * uw_ask_ratio * 2.0)
            return WickTrap(
                side="bear_trap",
                wick_pct=upper_wick_pct,
                wick_vol=uw_bid + uw_ask,
                body_vol=body_vol,
                wick_bid_ratio=uw_bid / uw_total,
                confidence=round(conf, 3),
            )
    return None


def wick_trap_signal(bars: list[Bar], fps: list[FootprintMatrix],
                     min_wick_pct: float = 0.30) -> WickTrap | None:
    """Check last 3 bars for any wick trap. Returns most recent match."""
    for bar, fp in zip(reversed(bars[-3:]), reversed(fps[-3:])):
        t = detect_wick_trap(bar, fp, min_wick_pct)
        if t is not None:
            return t
    return None
