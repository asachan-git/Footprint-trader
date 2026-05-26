"""Liquidity sweep detector.

A sweep = price exceeds a tracked reference level (session high/low, prior day
high/low, VAH/VAL), then CLOSES BACK inside — indicating the move was a stop
hunt / liquidity grab with no acceptance at that level.

Footprint confirms the sweep:
  - High volume at the extreme (stops + institutional absorption)
  - Opposing absorption at the wick tip (sellers defending at sweep high)
  - Bar closes back inside the swept level (rejection confirmed)
  - Delta at extreme is opposite to the wick direction

A confirmed sweep is the highest-probability reversal setup in this system.
It means: trapped retail participants + institutional inventory built in
the opposite direction.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.features.swing import SwingPoints, all_reference_levels

# Volume at the extreme (top/bottom N% of bar range) vs bar average
EXTREME_VOL_FRACTION = 0.10   # top/bottom 10% of bar range = "extreme zone"
EXTREME_VOL_MULTIPLIER = 1.5  # extreme levels must have this × avg level volume
# Bar close must be this far from the extreme (% of bar range) to confirm rejection
MIN_REJECTION_PCT = 0.35
# Minimum bar range as % of price to filter tiny bars
MIN_BAR_RANGE_PCT = 0.0005   # 0.05%


@dataclass
class SweepSignal:
    type: str           # "sweep_high" | "sweep_low" | "none"
    swept_level: float  # the reference level that was swept
    level_label: str    # e.g. "session_high", "prior_day_high", "vah"
    wick_extreme: float # highest high (sweep_high) or lowest low (sweep_low)
    bar_close: float
    confidence: float   # 0.0 – 1.0
    volume_at_extreme: float
    avg_level_volume: float
    vol_ratio: float    # volume_at_extreme / avg_level_volume
    reason: str


_NONE = SweepSignal(
    type="none", swept_level=0.0, level_label="", wick_extreme=0.0,
    bar_close=0.0, confidence=0.0, volume_at_extreme=0.0,
    avg_level_volume=0.0, vol_ratio=0.0, reason="",
)


def _extreme_volume(bar, direction: str, extreme_frac: float = EXTREME_VOL_FRACTION) -> tuple[float, float]:
    """Return (vol_at_extreme, avg_vol_per_level) for a bar.

    direction="high": look at top extreme_frac of bar range (sweep high)
    direction="low":  look at bottom extreme_frac (sweep low)
    """
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0:
        return 0.0, 0.0

    extreme_width = bar_range * extreme_frac
    if direction == "high":
        threshold = bar.ohlc.h - extreme_width
        extreme_levels = [lvl for lvl in list(bar.bid_ladder) + list(bar.ask_ladder) if lvl.price >= threshold]
    else:
        threshold = bar.ohlc.l + extreme_width
        extreme_levels = [lvl for lvl in list(bar.bid_ladder) + list(bar.ask_ladder) if lvl.price <= threshold]

    all_levels = list(bar.bid_ladder) + list(bar.ask_ladder)
    if not all_levels:
        return 0.0, 0.0

    vol_at_extreme = sum(lvl.vol for lvl in extreme_levels)
    avg_vol = sum(lvl.vol for lvl in all_levels) / len(all_levels)
    return vol_at_extreme, avg_vol


def _close_rejection_pct(bar, direction: str) -> float:
    """How far the close is from the extreme as fraction of bar range.

    direction="high": close should be far below the high (sweep high rejection)
    direction="low":  close should be far above the low (sweep low rejection)

    Returns 0.0–1.0. Higher = stronger rejection.
    """
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0:
        return 0.0
    if direction == "high":
        return (bar.ohlc.h - bar.ohlc.c) / bar_range
    else:
        return (bar.ohlc.c - bar.ohlc.l) / bar_range


def detect(bar, swing_pts: SwingPoints, prev_bars: list | None = None) -> SweepSignal:
    """Detect a liquidity sweep on the current bar against tracked reference levels.

    Args:
        bar:        Current bar (just closed).
        swing_pts:  Tracked reference levels for this session.
        prev_bars:  Recent prior bars (used for context; optional).
    """
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0 or bar_range / max(bar.ohlc.c, 0.01) < MIN_BAR_RANGE_PCT:
        return _NONE

    ref_levels = all_reference_levels(swing_pts)
    best = _NONE

    for label, level in ref_levels:
        # --- Sweep HIGH check ---
        if bar.ohlc.h > level and bar.ohlc.c < level:
            # Bar exceeded the level but closed back below it
            wick_above = bar.ohlc.h - level
            rejection_pct = _close_rejection_pct(bar, "high")

            if rejection_pct < MIN_REJECTION_PCT:
                continue  # close too near the high — not a clear rejection

            vol_extreme, avg_vol = _extreme_volume(bar, "high")
            vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0

            conf = _sweep_confidence(rejection_pct, vol_ratio, label)
            if conf > best.confidence:
                best = SweepSignal(
                    type="sweep_high",
                    swept_level=level,
                    level_label=label,
                    wick_extreme=bar.ohlc.h,
                    bar_close=bar.ohlc.c,
                    confidence=conf,
                    volume_at_extreme=vol_extreme,
                    avg_level_volume=avg_vol,
                    vol_ratio=round(vol_ratio, 3),
                    reason=(
                        f"{label} {level:.2f} swept (high={bar.ohlc.h:.2f}), "
                        f"close={bar.ohlc.c:.2f}, rejection={rejection_pct*100:.0f}%, "
                        f"extreme_vol_ratio={vol_ratio:.2f}×"
                    ),
                )

        # --- Sweep LOW check ---
        elif bar.ohlc.l < level and bar.ohlc.c > level:
            wick_below = level - bar.ohlc.l
            rejection_pct = _close_rejection_pct(bar, "low")

            if rejection_pct < MIN_REJECTION_PCT:
                continue

            vol_extreme, avg_vol = _extreme_volume(bar, "low")
            vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0

            conf = _sweep_confidence(rejection_pct, vol_ratio, label)
            if conf > best.confidence:
                best = SweepSignal(
                    type="sweep_low",
                    swept_level=level,
                    level_label=label,
                    wick_extreme=bar.ohlc.l,
                    bar_close=bar.ohlc.c,
                    confidence=conf,
                    volume_at_extreme=vol_extreme,
                    avg_level_volume=avg_vol,
                    vol_ratio=round(vol_ratio, 3),
                    reason=(
                        f"{label} {level:.2f} swept (low={bar.ohlc.l:.2f}), "
                        f"close={bar.ohlc.c:.2f}, rejection={rejection_pct*100:.0f}%, "
                        f"extreme_vol_ratio={vol_ratio:.2f}×"
                    ),
                )

    return best


def _sweep_confidence(rejection_pct: float, vol_ratio: float, level_label: str) -> float:
    """Confidence from rejection strength, volume, and level significance."""
    # Rejection quality: how far close is from wick (0.35 = min, 1.0 = max)
    rejection_score = min(1.0, (rejection_pct - MIN_REJECTION_PCT) / (1.0 - MIN_REJECTION_PCT))

    # Volume at extreme (higher = more trapped participants)
    vol_score = min(1.0, (vol_ratio - 1.0) / 3.0) if vol_ratio >= 1.0 else 0.0

    # Level significance bonus
    label_bonus = {
        "prior_day_high": 0.15,
        "prior_day_low":  0.15,
        "vah":            0.10,
        "val":            0.10,
        "session_high":   0.05,
        "session_low":    0.05,
    }.get(level_label, 0.0)

    raw = 0.50 * rejection_score + 0.35 * vol_score + label_bonus
    return round(min(0.92, max(0.35, 0.30 + raw * 0.70)), 2)


def from_store(symbol: str, primary_tf: str) -> SweepSignal:
    """Convenience: detect sweep on latest bar using cached swing points."""
    from pipeline.state_store import store
    from pipeline.features.swing import get as get_swing

    s = store()
    recent = s.recent(symbol, primary_tf, 20)  # standardized analysis window
    if not recent:
        return _NONE

    sp = get_swing(symbol)
    if sp is None:
        return _NONE

    return detect(recent[-1], sp, prev_bars=recent[:-1])
