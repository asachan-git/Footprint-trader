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

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal

from pipeline.features.swing import SwingPoints, all_reference_levels

SWEEP_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_log_lock = Lock()

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
    rejection_pct: float = 0.0   # bar rejection fraction when sweep fired
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
    classification: str = ""     # "reversal" | "liquidity_grab" | "stop_run" | "failed_sweep" | ""
    rejection_pct: float = 0.0   # bar rejection fraction when sweep fired (used for classification)
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
                # Classify based on pattern + delta + rejection strength
                if ev.classification == "":
                    if ev.pattern == "sweep_acceptance":
                        ev.classification = "failed_sweep"
                    elif not ev.delta_confirms:
                        # Opposite delta = stops cleared but no reversal participation
                        ev.classification = "stop_run"
                    elif ev.pattern == "sweep_reclaim" and ev.rejection_pct >= 0.55:
                        # Strong delta + strong reclaim + deep rejection = real reversal
                        ev.classification = "reversal"
                    elif ev.level_label in ("prior_day_high", "prior_day_low",
                                            "session_high", "session_low",
                                            "equal_high", "equal_low"):
                        ev.classification = "liquidity_grab"
                    else:
                        ev.classification = "reversal"  # default for delta-confirmed sweeps
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
            rejection_pct=sig.rejection_pct,
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


# ── Candle engulf sweep ────────────────────────────────────────────────────────

def detect_candle_engulf(bar, prev_bar) -> SweepSignal:
    """Single-bar candle sweep: wick sweeps prior H/L AND close engulfs prior body.

    Sweep HIGH: bar.high > prev.high AND bar.close < prev.low  (bearish engulf after sweep)
    Sweep LOW:  bar.low  < prev.low  AND bar.close > prev.high (bullish engulf after sweep)

    Returns _NONE if neither condition met.
    """
    if prev_bar is None:
        return _NONE
    bar_range = bar.ohlc.h - bar.ohlc.l
    if bar_range <= 0 or bar_range / max(bar.ohlc.c, 0.01) < MIN_BAR_RANGE_PCT:
        return _NONE

    prev_body_high = max(prev_bar.ohlc.o, prev_bar.ohlc.c)
    prev_body_low  = min(prev_bar.ohlc.o, prev_bar.ohlc.c)

    # Sweep HIGH + bearish engulf: wicks above prev high, closes below prev body low
    if bar.ohlc.h > prev_bar.ohlc.h and bar.ohlc.c < prev_body_low:
        rejection_pct = _close_rejection_pct(bar, "high")
        vol_extreme, avg_vol = _extreme_volume(bar, "high")
        vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0
        delta_ok = _delta_confirms_sweep(bar, "sweep_high")
        conf = _sweep_confidence(rejection_pct, vol_ratio, "candle_engulf_high")
        if not delta_ok:
            conf = round(conf * 0.5, 2)
        sig = SweepSignal(
            type="sweep_high",
            swept_level=prev_bar.ohlc.h,
            level_label="candle_engulf_high",
            wick_extreme=bar.ohlc.h,
            bar_close=bar.ohlc.c,
            confidence=conf,
            volume_at_extreme=vol_extreme,
            avg_level_volume=avg_vol,
            vol_ratio=round(vol_ratio, 3),
            reason=(
                f"engulf: swept prev_high={prev_bar.ohlc.h:.2f}, "
                f"close={bar.ohlc.c:.2f} < prev_body_low={prev_body_low:.2f}, "
                f"rejection={rejection_pct*100:.0f}%"
            ),
            delta_confirms=delta_ok,
            rejection_pct=rejection_pct,
            granularity="candle",
        )
        _register_new_sweep(bar.symbol if hasattr(bar, "symbol") else "", sig)
        return sig

    # Sweep LOW + bullish engulf: wicks below prev low, closes above prev body high
    if bar.ohlc.l < prev_bar.ohlc.l and bar.ohlc.c > prev_body_high:
        rejection_pct = _close_rejection_pct(bar, "low")
        vol_extreme, avg_vol = _extreme_volume(bar, "low")
        vol_ratio = vol_extreme / avg_vol if avg_vol > 0 else 0.0
        delta_ok = _delta_confirms_sweep(bar, "sweep_low")
        conf = _sweep_confidence(rejection_pct, vol_ratio, "candle_engulf_low")
        if not delta_ok:
            conf = round(conf * 0.5, 2)
        sig = SweepSignal(
            type="sweep_low",
            swept_level=prev_bar.ohlc.l,
            level_label="candle_engulf_low",
            wick_extreme=bar.ohlc.l,
            bar_close=bar.ohlc.c,
            confidence=conf,
            volume_at_extreme=vol_extreme,
            avg_level_volume=avg_vol,
            vol_ratio=round(vol_ratio, 3),
            reason=(
                f"engulf: swept prev_low={prev_bar.ohlc.l:.2f}, "
                f"close={bar.ohlc.c:.2f} > prev_body_high={prev_body_high:.2f}, "
                f"rejection={rejection_pct*100:.0f}%"
            ),
            delta_confirms=delta_ok,
            rejection_pct=rejection_pct,
            granularity="candle",
        )
        _register_new_sweep(bar.symbol if hasattr(bar, "symbol") else "", sig)
        return sig

    return _NONE


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
                    rejection_pct=rejection_pct,
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
                    rejection_pct=rejection_pct,
                    granularity="candle",
                )

    if best.type != "none":
        _register_new_sweep(swing_pts.symbol, best)

    # Also check candle engulf — if stronger, use it instead
    if prev_bars:
        prev_bar = prev_bars[-1]
        engulf = detect_candle_engulf(bar, prev_bar)
        if engulf.type != "none" and engulf.confidence > best.confidence:
            return engulf  # already registered inside detect_candle_engulf

    return best


def _sweep_confidence(rejection_pct: float, vol_ratio: float, level_label: str) -> float:
    rejection_score = min(1.0, (rejection_pct - MIN_REJECTION_PCT) / (1.0 - MIN_REJECTION_PCT))
    vol_score = min(1.0, (vol_ratio - 1.0) / 3.0) if vol_ratio >= 1.0 else 0.0
    label_bonus = {
        "equal_high":        0.20,   # EQH stop cluster — highest priority target
        "equal_low":         0.20,
        "prior_day_high":    0.15,
        "prior_day_low":     0.15,
        "vah":               0.10,
        "val":               0.10,
        "session_high":      0.05,
        "session_low":       0.05,
        "candle_engulf_high": 0.12,
        "candle_engulf_low":  0.12,
    }.get(level_label, 0.0)
    raw = 0.50 * rejection_score + 0.35 * vol_score + label_bonus
    return round(min(0.92, max(0.35, 0.30 + raw * 0.70)), 2)


def _sweep_log_path(symbol: str, tf: str = "1m") -> Path:
    return SWEEP_LOG_DIR / f"sweeps_{symbol}_{tf}.jsonl"


_LIQUIDITY_LABELS = (
    "prior_day_high", "prior_day_low", "session_high", "session_low",
    "equal_high", "equal_low",
)


def _classify_signal(sig: SweepSignal) -> str:
    """Derive sweep classification from signal fields.

    Priority order (was rejection-first, which mislabeled stop-cluster grabs as
    reversals and flooded the chart with REV):
      1. no delta confirm           → stop_run
      2. liquidity level (EQ/PDH/PDL/session) → liquidity_grab, even on strong
         rejection — sweeping a known stop cluster IS a grab, not a reversal.
      3. strong rejection (≥0.55) at a structural level → reversal
      4. weak rejection elsewhere   → failed_sweep (dimmed; was REV noise)
    """
    if not sig.delta_confirms:
        return "stop_run"
    if sig.level_label in _LIQUIDITY_LABELS:
        return "liquidity_grab"
    if sig.rejection_pct >= 0.55:
        return "reversal"
    return "failed_sweep"


def detect_swing_sweeps(raw_bars, swing_points) -> list[dict]:
    """Sweeps of the *same-TF displayed swing highs/lows*.

    For each swing pivot the chart draws, find the first later bar that wicks
    beyond the swing price and closes back inside (rejection ≥ MIN_REJECTION_PCT).
    Classified with the same logic as live sweeps so the dashboard renders them
    as REV/LG/SR/FS arrows. Returns log-schema dicts tagged ``source="swing"``.

    Unlike read_sweep_log (VP/session/prior-day levels from the live engine),
    these always sit on a swing the chart shows on this timeframe.
    """
    if not raw_bars or not swing_points:
        return []
    ts_to_idx = {b.close_ts: i for i, b in enumerate(raw_bars)}
    out: list[dict] = []
    for sp in swing_points:
        side  = sp.get("side")
        price = sp.get("price")
        sp_ts = sp.get("ts")
        if price is None or sp_ts is None or side not in ("high", "low"):
            continue
        start = ts_to_idx.get(sp_ts)
        if start is None:
            continue
        direction  = "high" if side == "high" else "low"
        sweep_type = "sweep_high" if side == "high" else "sweep_low"
        label      = "swing_high" if side == "high" else "swing_low"
        for b in raw_bars[start + 1:]:
            rng = b.ohlc.h - b.ohlc.l
            if rng <= 0 or rng / max(b.ohlc.c, 0.01) < MIN_BAR_RANGE_PCT:
                continue
            swept = (b.ohlc.h > price and b.ohlc.c < price) if side == "high" \
                    else (b.ohlc.l < price and b.ohlc.c > price)
            if not swept:
                continue
            rej = _close_rejection_pct(b, direction)
            if rej < MIN_REJECTION_PCT:
                continue
            vol_ext, avg_vol = _extreme_volume(b, direction)
            vol_ratio = vol_ext / avg_vol if avg_vol > 0 else 0.0
            delta_ok  = _delta_confirms_sweep(b, sweep_type)
            conf = _sweep_confidence(rej, vol_ratio, label)
            if not delta_ok:
                conf = round(conf * 0.5, 2)
            extreme = b.ohlc.h if side == "high" else b.ohlc.l
            sig = SweepSignal(
                type=sweep_type, swept_level=price, level_label=label,
                wick_extreme=extreme, bar_close=b.ohlc.c, confidence=conf,
                volume_at_extreme=vol_ext, avg_level_volume=avg_vol,
                vol_ratio=round(vol_ratio, 3), reason="swing-derived",
                delta_confirms=delta_ok, rejection_pct=rej, granularity="candle",
            )
            out.append({
                "ts": b.close_ts,
                "type": sweep_type,
                "swept_level": round(price, 4),
                "level_label": label,
                "wick_extreme": round(extreme, 4),
                "bar_close": round(b.ohlc.c, 4),
                "confidence": round(conf, 3),
                "delta_confirms": delta_ok,
                "rejection_pct": round(rej, 3),
                "granularity": "candle",
                "classification": _classify_signal(sig),
                "source": "swing",
            })
            break  # first sweep of this pivot only
    return out


def log_sweep(symbol: str, sig: SweepSignal, bar_ts: int, tf: str = "1m") -> None:
    """Append a detected sweep to the per-symbol per-TF JSONL log."""
    entry = {
        "ts": bar_ts,
        "type": sig.type,
        "swept_level": round(sig.swept_level, 4),
        "level_label": sig.level_label,
        "wick_extreme": round(sig.wick_extreme, 4),
        "bar_close": round(sig.bar_close, 4),
        "confidence": round(sig.confidence, 3),
        "delta_confirms": sig.delta_confirms,
        "rejection_pct": round(sig.rejection_pct, 3),
        "granularity": sig.granularity,
        "classification": _classify_signal(sig),
    }
    with _log_lock:
        with _sweep_log_path(symbol, tf).open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def read_sweep_log(symbol: str, since_ts: int = 0, tf: str = "1m") -> list[dict]:
    """Return all logged sweeps for symbol/TF since since_ts."""
    p = _sweep_log_path(symbol, tf)
    if not p.exists():
        return []
    out = []
    try:
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("ts", 0) >= since_ts:
                        # Re-derive classification from stored fields so historical
                        # sweeps reflect current _classify_signal logic (old log
                        # entries were tagged with the prior rejection-first rule).
                        e["classification"] = _classify_signal(SweepSignal(
                            type=e.get("type", "none"),
                            swept_level=e.get("swept_level", 0.0),
                            level_label=e.get("level_label", ""),
                            wick_extreme=e.get("wick_extreme", 0.0),
                            bar_close=e.get("bar_close", 0.0),
                            confidence=e.get("confidence", 0.0),
                            volume_at_extreme=0.0, avg_level_volume=0.0, vol_ratio=0.0,
                            reason="", delta_confirms=e.get("delta_confirms", True),
                            rejection_pct=e.get("rejection_pct", 0.0),
                            granularity=e.get("granularity", "candle"),
                        ))
                        out.append(e)
                except Exception:
                    pass
    except Exception:
        pass
    return out


_TF_BARS_PER_DAY: dict[str, int] = {
    "1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24,
}


def backfill_registry(symbol: str, primary_tf: str, days: int = 5) -> int:
    """Replay stored bars through sweep detector to populate registry and log.

    TF-aware: uses bars_per_day from _TF_BARS_PER_DAY.
    Builds rolling SwingPoints per bar with structural pivots.
    Skips timestamps already in log to avoid duplicates.
    Returns count of sweeps detected.
    """
    import time
    from pipeline.state_store import store as _store
    from pipeline.features.swing import build as swing_build

    bars_per_day = _TF_BARS_PER_DAY.get(primary_tf, 1440)
    since_ts = int(time.time()) - days * 86400
    s = _store()
    bars = s.recent(symbol, primary_tf, days * bars_per_day + 50)
    bars = [b for b in bars if b.close_ts >= since_ts]
    if len(bars) < 10:
        return 0

    # Load already-logged timestamps to avoid duplicates
    existing = {e["ts"] for e in read_sweep_log(symbol, since_ts=since_ts, tf=primary_tf)}

    n_detected = 0
    window = min(200, max(50, bars_per_day // 4))   # TF-scaled lookback window
    for i, bar in enumerate(bars):
        session_slice = bars[max(0, i - window): i + 1]
        sp = swing_build(symbol, primary_tf, session_slice)
        prev = bars[max(0, i - window): i]
        sig = detect(bar, sp, prev_bars=prev)
        tick_registry(symbol, bar)
        if sig.type != "none":
            if bar.close_ts not in existing:
                log_sweep(symbol, sig, bar.close_ts, tf=primary_tf)
                existing.add(bar.close_ts)
            n_detected += 1
    return n_detected


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
