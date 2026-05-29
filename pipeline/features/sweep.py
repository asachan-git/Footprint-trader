"""Liquidity sweep detector with persistent SweepRegistry.

A sweep = price exceeds a tracked reference level (session high/low, prior day
high/low, VAH/VAL), then CLOSES BACK inside — indicating the move was a stop
hunt / liquidity grab with no acceptance at that level.

Detection granularities:
  candle  — single-bar: high > level + close < level with ≥ 35% rejection
  range   — 3–5 bar rolling: any bar wick > level, latest bar closes < level
             (slow stop-sweep / grinding rejection) [TODO: Phase 3 extension]
  session — session-high updated above prior_day_high + closes below by session end
             [TODO: Phase 3 extension]

Follow-up patterns (tracked via SweepRegistry, populated next bar):
  sweep_reclaim    — sweep fires, next bar closes further INTO range (strong reversal)
  sweep_acceptance — sweep fires, next bar closes BACK BEYOND swept level (sweep failed)

Delta validation at wick:
  sweep_high: bid-side volume dominates at extreme 10% (sellers hit bids — bearish)
  sweep_low:  ask-side volume dominates at extreme 10% (buyers lift asks — bullish)
  If opposite side dominates → delta_confirms=False → confidence × 0.5

SweepRegistry keeps events alive for 20 bars. Eviction on:
  - Age > 20 bars
  - Opposite-direction sweep at same level
  - Sweep failure (price closes back through swept level 2+ bars)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

from pipeline.features.swing import SwingPoints, all_reference_levels

# Volume at the extreme (top/bottom N% of bar range) vs bar average
EXTREME_VOL_FRACTION = 0.10   # top/bottom 10% of bar range = "extreme zone"
EXTREME_VOL_MULTIPLIER = 1.5  # extreme levels must have this × avg level volume
# Bar close must be this far from the extreme (% of bar range) to confirm rejection
MIN_REJECTION_PCT = 0.35
# Minimum bar range as % of price to filter tiny bars
MIN_BAR_RANGE_PCT = 0.0005   # 0.05%
# Registry event lifetime in bars
SWEEP_EVENT_MAX_AGE = 20


@dataclass
class SweepSignal:
    type: str              # "sweep_high" | "sweep_low" | "none"
    swept_level: float     # the reference level that was swept
    level_label: str       # e.g. "session_high", "prior_day_high", "vah"
    wick_extreme: float    # highest high (sweep_high) or lowest low (sweep_low)
    bar_close: float
    confidence: float      # 0.0 – 1.0
    volume_at_extreme: float
    avg_level_volume: float
    vol_ratio: float       # volume_at_extreme / avg_level_volume
    reason: str
    delta_confirms: bool = True    # False → confidence × 0.5
    granularity: str = "candle"    # "candle" | "range" | "session"
    age_bars: int = 0              # bars since sweep fired


@dataclass
class SweepEvent:
    """Persistent registry entry for a sweep that fired."""
    sweep_type: str             # "sweep_high" | "sweep_low"
    swept_level: float
    level_label: str
    wick_extreme: float
    initial_close: float        # bar close when sweep fired
    confidence: float
    delta_confirms: bool
    granularity: str
    age_bars: int = 0
    pattern: str = ""           # "" | "sweep_reclaim" | "sweep_acceptance"
    stale: bool = False
    _consecutive_fails: int = field(default=0, repr=False)


_NONE = SweepSignal(
    type="none", swept_level=0.0, level_label="", wick_extreme=0.0,
    bar_close=0.0, confidence=0.0, volume_at_extreme=0.0,
    avg_level_volume=0.0, vol_ratio=0.0, reason="",
)

# ── Registry ───────────────────────────────────────────────────────────────────

_registry: dict[str, list[SweepEvent]] = {}  # symbol → list[SweepEvent]
_reg_lock = Lock()


def _evict(events: list[SweepEvent]) -> list[SweepEvent]:
    """Remove stale + aged-out events."""
    return [e for e in events if not e.stale and e.age_bars <= SWEEP_EVENT_MAX_AGE]


def _update_events(events: list[SweepEvent], current_bar) -> list[SweepEvent]:
    """Increment age, detect sweep_reclaim / sweep_acceptance, mark failures."""
    close = current_bar.ohlc.c
    updated = []
    for ev in events:
        ev.age_bars += 1
        if ev.pattern == "":
            # Check for follow-up pattern on the bar AFTER the sweep
            if ev.age_bars == 1:
                if ev.sweep_type == "sweep_high":
                    if close < ev.initial_close:
                        ev.pattern = "sweep_reclaim"
                    elif close > ev.swept_level:
                        ev.pattern = "sweep_acceptance"
                elif ev.sweep_type == "sweep_low":
                    if close > ev.initial_close:
                        ev.pattern = "sweep_reclaim"
                    elif close < ev.swept_level:
                        ev.pattern = "sweep_acceptance"
        # Failure check: price closes back through swept level 2+ consecutive bars
        if ev.sweep_type == "sweep_high" and close > ev.swept_level:
            ev._consecutive_fails += 1
        elif ev.sweep_type == "sweep_low" and close < ev.swept_level:
            ev._consecutive_fails += 1
        else:
            ev._consecutive_fails = 0
        if ev._consecutive_fails >= 2:
            ev.stale = True
        updated.append(ev)
    return updated


def _register_new_sweep(symbol: str, sig: SweepSignal) -> None:
    """Add or replace registry entry for a fresh sweep event."""
    with _reg_lock:
        evs = _registry.get(symbol, [])
        # Evict any existing event at the same level (opposite direction = invalidation)
        key = (sig.level_label, round(sig.swept_level, 2))
        evs = [e for e in evs
               if not (e.level_label == sig.level_label and
                       abs(e.swept_level - sig.swept_level) < 0.01)]
        evs.append(SweepEvent(
            sweep_type=sig.type,
            swept_level=sig.swept_level,
            level_label=sig.level_label,
            wick_extreme=sig.wick_extreme,
            initial_close=sig.bar_close,
            confidence=sig.confidence,
            delta_confirms=sig.delta_confirms,
            granularity=sig.granularity,
        ))
        _registry[symbol] = evs


def tick_registry(symbol: str, current_bar) -> None:
    """Advance all registry events by one bar (call every bar after detect)."""
    with _reg_lock:
        evs = _registry.get(symbol, [])
        evs = _update_events(evs, current_bar)
        evs = _evict(evs)
        _registry[symbol] = evs


def active_sweeps(symbol: str) -> list[SweepEvent]:
    """Return live (not stale, not aged-out) sweep events for symbol."""
    with _reg_lock:
        return list(_registry.get(symbol, []))


def reset_registry(symbol: str) -> None:
    """Clear all registry events (e.g. at session boundary)."""
    with _reg_lock:
        _registry.pop(symbol, None)


# ── Volume helpers ─────────────────────────────────────────────────────────────

def _extreme_volume(bar, direction: str, extreme_frac: float = EXTREME_VOL_FRACTION) -> tuple[float, float]:
    """Return (vol_at_extreme, avg_vol_per_level) for a bar."""
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0:
        return 0.0, 0.0

    extreme_width = bar_range * extreme_frac
    if direction == "high":
        threshold = bar.ohlc.h - extreme_width
        extreme_levels = [lvl for lvl in list(bar.bid_ladder) + list(bar.ask_ladder)
                          if lvl.price >= threshold]
    else:
        threshold = bar.ohlc.l + extreme_width
        extreme_levels = [lvl for lvl in list(bar.bid_ladder) + list(bar.ask_ladder)
                          if lvl.price <= threshold]

    all_levels = list(bar.bid_ladder) + list(bar.ask_ladder)
    if not all_levels:
        return 0.0, 0.0

    vol_at_extreme = sum(lvl.vol for lvl in extreme_levels)
    avg_vol = sum(lvl.vol for lvl in all_levels) / len(all_levels)
    return vol_at_extreme, avg_vol


def _delta_confirms_sweep(bar, sweep_type: str) -> bool:
    """Check if delta at wick extreme confirms the sweep direction.

    sweep_high: expect bid-side dominance at extreme (sellers hit bids → bearish)
    sweep_low:  expect ask-side dominance at extreme (buyers lift asks → bullish)
    """
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0:
        return True  # can't check → assume confirms

    extreme_width = bar_range * EXTREME_VOL_FRACTION
    if sweep_type == "sweep_high":
        threshold = bar.ohlc.h - extreme_width
        bid_vol = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price >= threshold)
        ask_vol = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price >= threshold)
        # Sellers dominated at the high → confirms bearish sweep
        return bid_vol >= ask_vol * 0.8  # bid_vol should be ≥ ask_vol (sellers dominant)
    else:  # sweep_low
        threshold = bar.ohlc.l + extreme_width
        bid_vol = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price <= threshold)
        ask_vol = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price <= threshold)
        # Buyers dominated at the low → confirms bullish sweep
        return ask_vol >= bid_vol * 0.8  # ask_vol should be ≥ bid_vol (buyers dominant)


def _close_rejection_pct(bar, direction: str) -> float:
    """How far the close is from the extreme as fraction of bar range."""
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0:
        return 0.0
    if direction == "high":
        return (bar.ohlc.h - bar.ohlc.c) / bar_range
    else:
        return (bar.ohlc.c - bar.ohlc.l) / bar_range


# ── Core detection ─────────────────────────────────────────────────────────────

def detect(bar, swing_pts: SwingPoints, prev_bars: list | None = None) -> SweepSignal:
    """Detect a candle-granularity liquidity sweep on the current bar.

    Populates SweepRegistry with confirmed sweeps.
    Call tick_registry() after this each bar to advance event ages.

    Returns the best SweepSignal detected (or _NONE).
    """
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0 or bar_range / max(bar.ohlc.c, 0.01) < MIN_BAR_RANGE_PCT:
        return _NONE

    ref_levels = all_reference_levels(swing_pts)
    best = _NONE

    for label, level in ref_levels:
        # --- Sweep HIGH check ---
        if bar.ohlc.h > level and bar.ohlc.c < level:
            rejection_pct = _close_rejection_pct(bar, "high")
            if rejection_pct < MIN_REJECTION_PCT:
                continue

            vol_extreme, avg_vol = _extreme_volume(bar, "high")
            vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0
            delta_ok = _delta_confirms_sweep(bar, "sweep_high")
            conf = _sweep_confidence(rejection_pct, vol_ratio, label)
            if not delta_ok:
                conf = round(conf * 0.5, 2)

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
                        f"vol_ratio={vol_ratio:.2f}× delta_confirms={delta_ok}"
                    ),
                    delta_confirms=delta_ok,
                    granularity="candle",
                )

        # --- Sweep LOW check ---
        elif bar.ohlc.l < level and bar.ohlc.c > level:
            rejection_pct = _close_rejection_pct(bar, "low")
            if rejection_pct < MIN_REJECTION_PCT:
                continue

            vol_extreme, avg_vol = _extreme_volume(bar, "low")
            vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0
            delta_ok = _delta_confirms_sweep(bar, "sweep_low")
            conf = _sweep_confidence(rejection_pct, vol_ratio, label)
            if not delta_ok:
                conf = round(conf * 0.5, 2)

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
                        f"vol_ratio={vol_ratio:.2f}× delta_confirms={delta_ok}"
                    ),
                    delta_confirms=delta_ok,
                    granularity="candle",
                )

    if best.type != "none":
        _register_new_sweep(swing_pts.symbol, best)

    return best


def _sweep_confidence(rejection_pct: float, vol_ratio: float, level_label: str) -> float:
    rejection_score = min(1.0, (rejection_pct - MIN_REJECTION_PCT) / (1.0 - MIN_REJECTION_PCT))
    vol_score = min(1.0, (vol_ratio - 1.0) / 3.0) if vol_ratio >= 1.0 else 0.0
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
    """Convenience: detect sweep on latest bar + advance registry one tick.

    Returns the best sweep on the latest bar (or _NONE).
    Call this once per bar close. Use active_sweeps(symbol) to read registry.
    """
    from pipeline.state_store import store
    from pipeline.features.swing import get as get_swing

    s = store()
    recent = s.recent(symbol, primary_tf, 20)
    if not recent:
        return _NONE

    sp = get_swing(symbol)
    if sp is None:
        return _NONE

    current = recent[-1]
    sig = detect(current, sp, prev_bars=recent[:-1])
    # Advance existing registry events (age + follow-up pattern detection)
    # Skip the bar we just registered (it starts at age_bars=0)
    tick_registry(symbol, current)
    return sig
