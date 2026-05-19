"""Trading session detection + HTF SMA bias.

Session windows (UTC):
  Asia:             00:00 - 07:00  — low liquidity (esp. for gold)
  London:           07:00 - 12:00  — high activity
  London+NY Overlap: 12:00 - 16:00 — highest liquidity, best setups
  NY:               16:00 - 21:00  — good activity
  Off-hours:        21:00 - 24:00  — low liquidity for gold; crypto still active

Per-instrument active hours:
  BTCUSDT:  24/7 — crypto never sleeps, always in_active_hours=True
  XAUTUSDT: 22:00 UTC → 21:00 UTC (23h) — off only 21:00-22:00 UTC
             (= 03:30 IST → 02:30 IST, 1h break between NY close and Asian gold open)

HTF SMA bias: daily close vs 20-bar SMA on daily bars.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Per-symbol active hour ranges (UTC).
# Tuple: (start_inclusive, end_exclusive) or None = always active.
# For XAUTUSDT the break is 21:00-22:00 UTC, so everything EXCEPT that hour is active.
_SYMBOL_INACTIVE_HOURS: dict[str, set[int]] = {
    "BTCUSDT":  set(),            # never inactive
    "XAUTUSDT": {21},             # only 21:00-22:00 UTC is off (NY close to Asian gold open)
}


@dataclass(frozen=True)
class SessionContext:
    session: str           # "Asia" | "London" | "Overlap" | "NY" | "Off"
    in_active_hours: bool  # True when the symbol is in its active trading window
    utc_hour: int


def current_session(ts: int | None = None, symbol: str | None = None) -> SessionContext:
    """Return session context for a UNIX timestamp (default: now).

    Pass symbol to get the correct per-instrument in_active_hours.
    Without symbol: uses the generic Forex/equity schedule (off during Asia + Off).
    """
    utc_hour = int(time.gmtime(ts or time.time()).tm_hour)
    if 0 <= utc_hour < 7:
        session = "Asia"
    elif 7 <= utc_hour < 12:
        session = "London"
    elif 12 <= utc_hour < 16:
        session = "Overlap"
    elif 16 <= utc_hour < 21:
        session = "NY"
    else:
        session = "Off"

    if symbol is not None and symbol in _SYMBOL_INACTIVE_HOURS:
        active = utc_hour not in _SYMBOL_INACTIVE_HOURS[symbol]
    else:
        # Generic schedule: active during London + Overlap + NY
        active = session in ("London", "Overlap", "NY")

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
