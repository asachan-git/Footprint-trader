"""Zone collector — aggregates important price levels for grid placement.

Sources ranked by institutional significance:
  1.0  VP HVN (high-volume node — proven S/R)
  0.9  VP POC, naked POC
  0.8  VP VAH / VAL (value area boundaries)
  0.75 Swing session high/low, prior day high/low
  0.7  Unfilled FVGs (price inefficiency — tends to fill)
  0.6  CVD swing highs/lows
  0.5  VP LVN mid (fast-move zone — price accelerates through)

Returns zones sorted by distance from anchor (nearest first), deduplicated,
filtered to the correct side (above for short, below for long).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


@dataclass
class Zone:
    price: float
    source: str
    strength: float  # 0..1


def collect(
    symbol: str,
    direction: Literal["long", "short"],
    anchor: float,
    htf_bars: list[Bar] | None = None,
    n: int = 5,
    min_gap: float = 0.0,
) -> list[Zone]:
    """Return up to n zones on the correct side of anchor, nearest-first.

    min_gap: minimum price distance between zones after dedup.
             Defaults to 0 (caller controls via ATR or fixed pips).
    """
    raw: list[Zone] = []

    # ── VP levels ──────────────────────────────────────────────────────────
    try:
        from pipeline.features.vp_cache import get as vp_get
        for period in ("daily", "weekly"):
            vp = vp_get(symbol, period)
            if not vp:
                continue
            w = 0.9 if period == "daily" else 0.7
            for key, s in (("poc", w), ("vah", 0.8 * w), ("val", 0.8 * w), ("naked_poc", w + 0.05)):
                v = vp.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    raw.append(Zone(price=float(v), source=f"vp_{key}_{period}", strength=s))
            # HVN zones — use mid, highest strength
            for hvn in vp.get("hvn_zones") or []:
                mid = (hvn["low"] + hvn["high"]) / 2
                raw.append(Zone(price=mid, source=f"hvn_{period}", strength=1.0))
            # LVN zones — use mid, moderate strength (fast-move target)
            for lvn in vp.get("lvn_zones") or []:
                mid = (lvn["low"] + lvn["high"]) / 2
                raw.append(Zone(price=mid, source=f"lvn_{period}", strength=0.5))
    except Exception:
        pass

    # ── Swing levels ───────────────────────────────────────────────────────
    try:
        from pipeline.features.swing import get as swing_get, all_reference_levels
        sp = swing_get(symbol)
        if sp:
            _strengths = {
                "session_high": 0.75, "session_low": 0.75,
                "prior_day_high": 0.75, "prior_day_low": 0.75,
                "vah": 0.80, "val": 0.80,
                "cvd_swing_high": 0.60, "cvd_swing_low": 0.60,
            }
            for label, price in all_reference_levels(sp):
                raw.append(Zone(price=float(price), source=label,
                                strength=_strengths.get(label, 0.55)))
    except Exception:
        pass

    # ── FVG levels ─────────────────────────────────────────────────────────
    if htf_bars:
        try:
            from pipeline.features.fvg import detect_fvgs
            for fvg in detect_fvgs(htf_bars, max_age_bars=200):
                # Use zone entry edge (nearest price from inside the FVG)
                price = fvg.low if fvg.side == "bull" else fvg.high
                raw.append(Zone(price=float(price), source=f"fvg_{fvg.side}", strength=0.70))
        except Exception:
            pass

    # ── Filter to correct side ─────────────────────────────────────────────
    if direction == "short":
        candidates = [z for z in raw if z.price > anchor]
    else:
        candidates = [z for z in raw if z.price < anchor]

    if not candidates:
        return []

    # ── Sort by distance (nearest first) ──────────────────────────────────
    candidates.sort(key=lambda z: abs(z.price - anchor))

    # ── Deduplicate: merge zones within min_gap ────────────────────────────
    if min_gap > 0:
        merged: list[Zone] = []
        for z in candidates:
            if merged and abs(z.price - merged[-1].price) <= min_gap:
                # Keep the stronger one
                if z.strength > merged[-1].strength:
                    merged[-1] = z
            else:
                merged.append(z)
        candidates = merged

    return candidates[:n]


def _all_zones(
    symbol: str,
    htf_bars: list[Bar] | None = None,
) -> list[Zone]:
    """Return every strong zone (HVN/POC/VAH/VAL/FVG/swings), no side filter, no dedup.
    Used by cycle_manager for TP shrink + SL trail.
    """
    out: list[Zone] = []
    try:
        from pipeline.features.vp_cache import get as vp_get
        for period in ("daily", "weekly"):
            vp = vp_get(symbol, period)
            if not vp:
                continue
            w = 0.9 if period == "daily" else 0.7
            for key, s in (("poc", w), ("vah", 0.8 * w), ("val", 0.8 * w),
                           ("naked_poc", w + 0.05)):
                v = vp.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    out.append(Zone(price=float(v), source=f"vp_{key}_{period}", strength=s))
            for hvn in vp.get("hvn_zones") or []:
                mid = (hvn["low"] + hvn["high"]) / 2
                out.append(Zone(price=mid, source=f"hvn_{period}", strength=1.0))
            for lvn in vp.get("lvn_zones") or []:
                mid = (lvn["low"] + lvn["high"]) / 2
                out.append(Zone(price=mid, source=f"lvn_{period}", strength=0.5))
    except Exception:
        pass
    try:
        from pipeline.features.swing import get as swing_get, all_reference_levels
        sp = swing_get(symbol)
        if sp:
            for label, price in all_reference_levels(sp):
                out.append(Zone(price=float(price), source=label, strength=0.75))
    except Exception:
        pass
    if htf_bars:
        try:
            from pipeline.features.fvg import detect_fvgs
            for fvg in detect_fvgs(htf_bars, max_age_bars=200):
                price = fvg.low if fvg.side == "bull" else fvg.high
                out.append(Zone(price=float(price), source=f"fvg_{fvg.side}", strength=0.70))
        except Exception:
            pass
    return out


def nearest_strong_zone_toward(
    symbol: str,
    current_tp: float,
    avg_entry: float,
    side: Literal["long", "short"],
    htf_bars: list[Bar] | None = None,
    min_strength: float = 0.6,
) -> Zone | None:
    """For TP shrinking on adverse fill — find a strong zone CLOSER to avg
    than current_tp but still in the profit direction.

    LONG: TP is above avg. Shrink TP = lower it, but stay above avg.
          Returns highest zone with avg < z.price < current_tp.
    SHORT: TP is below avg. Shrink TP = raise it, but stay below avg.
          Returns lowest zone with current_tp < z.price < avg.
    """
    zones = [z for z in _all_zones(symbol, htf_bars) if z.strength >= min_strength]
    if side == "long":
        cand = [z for z in zones if avg_entry < z.price < current_tp]
        return max(cand, key=lambda z: (z.price, z.strength)) if cand else None
    cand = [z for z in zones if current_tp < z.price < avg_entry]
    return min(cand, key=lambda z: (z.price, -z.strength)) if cand else None


def nearest_strong_zone_beyond(
    symbol: str,
    current_price: float,
    current_sl: float,
    side: Literal["long", "short"],
    htf_bars: list[Bar] | None = None,
    min_strength: float = 0.6,
) -> Zone | None:
    """For SL trailing on favorable move — find a strong zone BETWEEN
    current_sl and current_price that the SL can advance to (locks profit).

    LONG: SL is below price. Trail SL = move it UP, but keep below price.
          Returns highest zone with current_sl < z.price < current_price.
    SHORT: SL is above price. Trail SL = move it DOWN, but keep above price.
          Returns lowest zone with current_price < z.price < current_sl.
    """
    zones = [z for z in _all_zones(symbol, htf_bars) if z.strength >= min_strength]
    if side == "long":
        cand = [z for z in zones if current_sl < z.price < current_price]
        return max(cand, key=lambda z: z.price) if cand else None
    cand = [z for z in zones if current_price < z.price < current_sl]
    return min(cand, key=lambda z: z.price) if cand else None
