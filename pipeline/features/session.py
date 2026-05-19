"""Trading session detection + HTF SMA bias.

Session windows (UTC):
  Asia:             00:00 - 07:00  — low liquidity (esp. for gold)
  London:           07:00 - 12:00  — high activity
  London+NY Overlap: 12:00 - 16:00 — highest liquidity, best setups
  NY:               16:00 - 21:00  — good activity
  Off-hours:        21:00 - 24:00  — low liquidity

HTF SMA bias: daily close vs 20-bar SMA on daily bars.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    session: str           # "Asia" | "London" | "Overlap" | "NY" | "Off"
    in_active_hours: bool  # True during London + Overlap + NY
    utc_hour: int


def current_session(ts: int | None = None) -> SessionContext:
    """Return session context for a UNIX timestamp (default: now)."""
    utc_hour = int(time.gmtime(ts or time.time()).tm_hour)
    if 0 <= utc_hour < 7:
        session, active = "Asia", False
    elif 7 <= utc_hour < 12:
        session, active = "London", True
    elif 12 <= utc_hour < 16:
        session, active = "Overlap", True
    elif 16 <= utc_hour < 21:
        session, active = "NY", True
    else:
        session, active = "Off", False
    return SessionContext(session=session, in_active_hours=active, utc_hour=utc_hour)


def sma(values: list[float], n: int) -> float | None:
    """Simple moving average of last N values."""
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def htf_bias(daily_bars) -> dict:
    """Compute daily trend bias from stored daily bars.

    Returns dict: {sma_20, close, bias: 'bullish'|'bearish'|'neutral'}
    """
    if not daily_bars:
        return {"sma_20": None, "close": None, "bias": "neutral"}

    closes = [b.ohlc.c for b in daily_bars]
    latest_close = closes[-1]
    sma_20 = sma(closes, 20)

    if sma_20 is None:
        bias = "neutral"
    elif latest_close > sma_20 * 1.001:
        bias = "bullish"
    elif latest_close < sma_20 * 0.999:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "sma_20": round(sma_20, 2) if sma_20 else None,
        "close": latest_close,
        "bias": bias,
    }
