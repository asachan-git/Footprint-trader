"""Session swing point tracker.

Tracks significant price levels that accumulate over the session and persist
as reference levels for sweep detection and grid placement:

  - Session developing high / low (updates every bar)
  - Prior day high / low (fixed at session open, from VP history)
  - CVD-confirmed swing highs / lows (wave structure levels)
  - Value area boundaries (VAH / VAL from daily VP)

Updated every bar from ingest. State is per-symbol in memory (not persisted —
rebuilds from state_store bars on server restart).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from pipeline.features.wave import detect_swing_points


@dataclass
class SwingPoints:
    symbol: str
    session_high: float | None       # highest high since session open
    session_low:  float | None       # lowest low since session open
    prior_day_high: float | None     # yesterday's high (from VP history)
    prior_day_low:  float | None     # yesterday's low (from VP history)
    vah: float | None                # value area high (daily VP)
    val: float | None                # value area low (daily VP)
    cvd_swing_highs: list[float]     # CVD-confirmed local swing highs
    cvd_swing_lows:  list[float]     # CVD-confirmed local swing lows
    updated_ts: int = 0


def _extract_prior_day(symbol: str) -> tuple[float | None, float | None]:
    """Get prior day high/low from VP history cache."""
    try:
        from pipeline.features.vp_cache import get_history
        hist = get_history(symbol, "daily", n=2)
        if len(hist) >= 2:
            prev = hist[-2]  # second most recent = prior day
            return prev.get("price_range_high"), prev.get("price_range_low")
    except Exception:
        pass
    return None, None


def _extract_vah_val(symbol: str) -> tuple[float | None, float | None]:
    """Get today's VAH/VAL from VP cache."""
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily")
        if vp:
            return vp.get("vah"), vp.get("val")
    except Exception:
        pass
    return None, None


def build(symbol: str, primary_tf: str, session_bars: list) -> SwingPoints:
    """Build SwingPoints from current session bars + VP cache data."""
    session_high = max((b.ohlc.h for b in session_bars), default=None)
    session_low  = min((b.ohlc.l for b in session_bars), default=None)

    prior_high, prior_low = _extract_prior_day(symbol)
    vah, val = _extract_vah_val(symbol)

    # CVD-confirmed swing points from wave module's swing detector
    cvd_highs: list[float] = []
    cvd_lows:  list[float] = []
    if len(session_bars) >= 6:
        s_highs, s_lows = detect_swing_points(session_bars)
        cvd_highs = [p for _, p in s_highs]
        cvd_lows  = [p for _, p in s_lows]

    return SwingPoints(
        symbol=symbol,
        session_high=session_high,
        session_low=session_low,
        prior_day_high=prior_high,
        prior_day_low=prior_low,
        vah=vah,
        val=val,
        cvd_swing_highs=cvd_highs,
        cvd_swing_lows=cvd_lows,
        updated_ts=int(time.time()),
    )


def all_reference_levels(sp: SwingPoints) -> list[tuple[str, float]]:
    """Return all tracked levels as (label, price) sorted by price descending.

    Used by sweep detector to iterate over all levels that could be swept.
    """
    levels: list[tuple[str, float]] = []
    for label, val in [
        ("session_high",    sp.session_high),
        ("session_low",     sp.session_low),
        ("prior_day_high",  sp.prior_day_high),
        ("prior_day_low",   sp.prior_day_low),
        ("vah",             sp.vah),
        ("val",             sp.val),
    ]:
        if val is not None:
            levels.append((label, val))
    for p in sp.cvd_swing_highs:
        levels.append(("cvd_swing_high", p))
    for p in sp.cvd_swing_lows:
        levels.append(("cvd_swing_low", p))
    return sorted(levels, key=lambda x: x[1], reverse=True)


# --- In-memory store updated on each ingest ---

_swing_cache: dict[str, SwingPoints] = {}
_lock = Lock()


def update(symbol: str, primary_tf: str, session_start_ts: int) -> SwingPoints:
    """Update and return SwingPoints for symbol from state_store."""
    from pipeline.state_store import store
    s = store()
    all_bars = s.recent(symbol, primary_tf, 2000)
    session_bars = [b for b in all_bars if b.close_ts >= session_start_ts]

    sp = build(symbol, primary_tf, session_bars)
    with _lock:
        _swing_cache[symbol] = sp
    return sp


def get(symbol: str) -> SwingPoints | None:
    """Return cached SwingPoints for symbol (None if not yet computed)."""
    return _swing_cache.get(symbol)
