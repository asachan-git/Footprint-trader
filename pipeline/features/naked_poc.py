"""Naked POC tracker — untested prior-session POCs as high-probability TP magnets.

A "naked POC" is a prior session's Point of Control that price has NOT yet
revisited since that session closed. Price gravitates toward these levels
because they represent unresolved institutional volume.

Usage:
    from pipeline.features.naked_poc import get_naked_pocs, is_naked

    levels = get_naked_pocs("XAUTUSDT", current_price=4520.0)
    # → [NakedPOC(price=4499.0, session='daily', age_sessions=2, ...)]

    # Use as TP targets in tp_resolver:
    for n in levels:
        candidates.append(TPCandidate(price=n.price, source=f"naked_poc_{n.session}"))

Design:
    - Looks back max_sessions daily + max_sessions weekly VP snapshots
    - A POC is "visited" if any bar in recent price history came within
      touch_tol × ATR of the level (same convention as va_touch)
    - Returns levels sorted by distance from current_price (nearest first)
    - Daily POC age > max_age_sessions sessions = skip (too stale)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NakedPOC:
    price: float
    session: Literal["daily", "weekly"]
    age_sessions: int       # how many sessions ago this POC formed
    period_key: str         # YYYY-MM-DD or YYYY-Www
    distance: float         # abs(price - current_price)


def get_naked_pocs(
    symbol: str,
    current_price: float,
    *,
    max_sessions: int = 5,
    touch_tol_atr_mult: float = 0.3,
    max_age_sessions: int = 10,
    min_strength: float = 0.0,     # reserved, not yet used
) -> list[NakedPOC]:
    """Return unvisited prior-session POCs, nearest first.

    Checks daily (last max_sessions) and weekly (last 3) snapshots.
    A POC is "visited" if recent bars came within touch_tol_atr_mult × ATR15m.
    Current session's POC is excluded (it's covered by vp_cache.get() live level).
    """
    try:
        from pipeline.features.vp_cache import get_history as _hist, get as _get
        from pipeline.features.atr import atr_from_store
        from pipeline.state_store import store as _store
    except Exception:
        return []

    try:
        atr_val = atr_from_store(symbol, "15m", period=14) or 1.0
        tol = touch_tol_atr_mult * atr_val
    except Exception:
        tol = 1.0

    # Collect recent bar closes to check "visited"
    try:
        recent_bars = _store().recent(symbol, "1m", 500)
        recent_highs = [b.ohlc.h for b in recent_bars]
        recent_lows = [b.ohlc.l for b in recent_bars]
    except Exception:
        recent_highs = []
        recent_lows = []

    def _was_visited(poc_price: float) -> bool:
        """True if any recent bar touched within tol of the POC."""
        for h, l in zip(recent_highs, recent_lows):
            if l - tol <= poc_price <= h + tol:
                return True
        return False

    # Current session POC keys to exclude
    try:
        current_daily = _get(symbol, "daily") or {}
        current_weekly = _get(symbol, "weekly") or {}
        current_daily_poc = current_daily.get("poc")
        current_weekly_poc = current_weekly.get("poc")
    except Exception:
        current_daily_poc = None
        current_weekly_poc = None

    naked: list[NakedPOC] = []

    for period, n_look in (("daily", max_sessions + 1), ("weekly", 3 + 1)):
        try:
            history = _hist(symbol, period, n=n_look)
        except Exception:
            continue

        # Exclude current session (last entry)
        prior = history[:-1] if len(history) > 1 else []

        for i, snap in enumerate(reversed(prior)):
            age = i + 1
            if age > max_age_sessions:
                break
            poc = snap.get("poc")
            if poc is None or poc <= 0:
                continue
            # Skip if same price as current session POC
            current_poc = current_daily_poc if period == "daily" else current_weekly_poc
            if current_poc is not None and abs(poc - current_poc) < tol:
                continue
            if _was_visited(poc):
                continue
            naked.append(NakedPOC(
                price=float(poc),
                session=period,
                age_sessions=age,
                period_key=snap.get("period_key", ""),
                distance=abs(float(poc) - current_price),
            ))

    naked.sort(key=lambda n: n.distance)
    return naked


def is_naked(symbol: str, price: float, *, touch_tol_atr_mult: float = 0.3) -> bool:
    """Quick check: is this price an active naked POC?"""
    levels = get_naked_pocs(symbol, price, touch_tol_atr_mult=touch_tol_atr_mult)
    try:
        from pipeline.features.atr import atr_from_store
        tol = touch_tol_atr_mult * (atr_from_store(symbol, "15m", period=14) or 1.0)
    except Exception:
        tol = 1.0
    return any(abs(n.price - price) <= tol for n in levels)
