"""ChoCh (Change of Character) detector — market structure flip.

A ChoCh occurs when an established trend's structure breaks:
  - Uptrend (HH/HL) → ChoCh-bear: new low BELOW the last confirmed HL
  - Downtrend (LL/LH) → ChoCh-bull: new high ABOVE the last confirmed LH

Swing detection: a bar is a swing high if its high is strictly greater than the
N bars before AND after (N=2 default = 5-bar fractal). Symmetric for swing low.

Returns the latest ChoCh event found within the lookback window. None = no flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


@dataclass(frozen=True)
class SwingPoint:
    idx: int          # index into the bars list
    bar_id: str
    ts: int
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class ChoChEvent:
    direction: Literal["bull", "bear"]  # post-ChoCh trend direction
    broken_level: float                  # the HL (bear ChoCh) or LH (bull ChoCh) that was breached
    broken_at_bar_id: str
    broken_at_ts: int
    last_trend: Literal["up", "down"]    # pre-ChoCh trend


def detect_swing_points(bars: list[Bar], n: int = 2) -> list[SwingPoint]:
    """5-bar fractal swing detection. n=2 → 2-left + 2-right."""
    swings: list[SwingPoint] = []
    if len(bars) < (2 * n + 1):
        return swings
    for i in range(n, len(bars) - n):
        win = bars[i - n: i + n + 1]
        h = bars[i].ohlc.h
        l = bars[i].ohlc.l
        if all(h > b.ohlc.h for j, b in enumerate(win) if j != n):
            swings.append(SwingPoint(idx=i, bar_id=bars[i].bar_id, ts=bars[i].close_ts, price=h, kind="high"))
        if all(l < b.ohlc.l for j, b in enumerate(win) if j != n):
            swings.append(SwingPoint(idx=i, bar_id=bars[i].bar_id, ts=bars[i].close_ts, price=l, kind="low"))
    return sorted(swings, key=lambda s: s.idx)


def _classify_trend(swings: list[SwingPoint]) -> Literal["up", "down", "none"]:
    """Look at last 4 swings to classify trend. HH+HL = up. LL+LH = down."""
    if len(swings) < 4:
        return "none"
    last4 = swings[-4:]
    highs = [s for s in last4 if s.kind == "high"]
    lows = [s for s in last4 if s.kind == "low"]
    if len(highs) >= 2 and len(lows) >= 2:
        # Uptrend: latest high > prior high AND latest low > prior low
        if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
            return "up"
        if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
            return "down"
    return "none"


def detect_choch(bars: list[Bar], n: int = 2, lookback_bars: int = 200) -> ChoChEvent | None:
    """Detect the latest ChoCh event within `lookback_bars` of bars.

    Returns None if no structural flip.
    """
    if len(bars) < 2 * n + 4:
        return None
    window = bars[-lookback_bars:] if len(bars) > lookback_bars else bars
    swings = detect_swing_points(window, n=n)
    if len(swings) < 4:
        return None
    trend = _classify_trend(swings[:-1]) if len(swings) >= 5 else _classify_trend(swings)
    if trend == "none":
        return None

    # Look at bars AFTER the last confirmed swing to see if the structure broke
    last_swing = swings[-1]
    later_bars = window[last_swing.idx + 1:]
    if not later_bars:
        return None

    if trend == "up":
        # Up trend: confirmed by HH/HL. ChoCh-bear if any bar closes below last HL.
        last_hl = next((s for s in reversed(swings) if s.kind == "low"), None)
        if last_hl is None:
            return None
        broken = next((b for b in later_bars if b.ohlc.c < last_hl.price), None)
        if broken:
            return ChoChEvent(
                direction="bear", broken_level=last_hl.price,
                broken_at_bar_id=broken.bar_id, broken_at_ts=broken.close_ts,
                last_trend="up",
            )

    if trend == "down":
        last_lh = next((s for s in reversed(swings) if s.kind == "high"), None)
        if last_lh is None:
            return None
        broken = next((b for b in later_bars if b.ohlc.c > last_lh.price), None)
        if broken:
            return ChoChEvent(
                direction="bull", broken_level=last_lh.price,
                broken_at_bar_id=broken.bar_id, broken_at_ts=broken.close_ts,
                last_trend="down",
            )
    return None
