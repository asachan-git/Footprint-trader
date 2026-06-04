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


def impulse_leg(bars: list[Bar], event: ChoChEvent, n: int = 2) -> tuple[float, float, int] | None:
    """Resolve the impulse leg that produced a ChoCh, for Fib projection.

    The leg runs from the swing ORIGIN (the pivot the impulse departed from) to the
    impulse EXTREME (the furthest price reached through the structure break):
      - bull ChoCh: origin = last swing LOW at/before the break, extreme = highest
        high from that low to now (impulse top).
      - bear ChoCh: origin = last swing HIGH at/before the break, extreme = lowest
        low from that high to now (impulse bottom).

    Returns (origin_price, extreme_price, break_idx) — all on the SAME `bars` list
    passed here — or None if anchors can't be resolved. Pass the same window used for
    detect_choch so indices line up.
    """
    if not bars:
        return None
    brk_idx = next((i for i, b in enumerate(bars) if b.bar_id == event.broken_at_bar_id), None)
    if brk_idx is None:
        return None
    swings = detect_swing_points(bars, n=n)
    want = "low" if event.direction == "bull" else "high"
    origin = next((s for s in reversed(swings) if s.kind == want and s.idx <= brk_idx), None)
    if origin is None:
        return None
    seg = bars[origin.idx:]
    if not seg:
        return None
    extreme = (max(b.ohlc.h for b in seg) if event.direction == "bull"
               else min(b.ohlc.l for b in seg))
    return origin.price, extreme, brk_idx


@dataclass(frozen=True)
class WaveStructure:
    side: Literal["long", "short"]   # continuation direction (with the trend)
    origin: float                     # wave-1 start  (bull: L0 low,  short: H0 high)
    origin_idx: int
    impulse: float                    # wave-1 extreme(bull: H1 = HH, short: L1 = LL)
    impulse_idx: int
    pullback: float                   # wave-2 end / SL pivot (bull: L1 = HL, short: H1 = LH)
    pullback_idx: int


def continuation_leg(bars: list[Bar], n: int = 2) -> WaveStructure | None:
    """Latest CONFIRMED two-wave trend structure ready for a 3rd-wave entry.

    Bull (uptrend): a Higher-High then a Higher-Low — swings end ...H0, L0, H1, L1
    with H1>H0 (HH) and L1>L0 (HL). wave-1 = L0→H1, wave-2 = H1→L1 (pullback), the
    3rd wave is the continuation up. origin=L0, impulse=H1, pullback=L1.
    Short (downtrend): a Lower-Low then a Lower-High — ...L0, H0, L1, H1 with L1<L0
    (LL) and H1<H0 (LH). origin=H0, impulse=L1, pullback=H1.

    CAUSAL: needs the pullback pivot fractal-confirmed (n bars to its right), so the
    structure is only reported once wave-2 has printed. None if not present.
    """
    swings = detect_swing_points(bars, n=n)
    if len(swings) < 4:
        return None
    last = swings[-1]

    def _prev(kind: str, before_idx: int):
        return next((s for s in reversed(swings) if s.kind == kind and s.idx < before_idx), None)

    if last.kind == "low":            # bull: HL just formed
        L1 = last
        H1 = _prev("high", L1.idx)
        if H1 is None:
            return None
        L0 = _prev("low", H1.idx)
        H0 = _prev("high", L0.idx) if L0 else None
        if not (L0 and H0):
            return None
        if not (H1.price > H0.price and L1.price > L0.price):   # HH and HL
            return None
        return WaveStructure("long", L0.price, L0.idx, H1.price, H1.idx, L1.price, L1.idx)

    if last.kind == "high":           # short: LH just formed
        H1 = last
        L1 = _prev("low", H1.idx)
        if L1 is None:
            return None
        H0 = _prev("high", L1.idx)
        L0 = _prev("low", H0.idx) if H0 else None
        if not (H0 and L0):
            return None
        if not (L1.price < L0.price and H1.price < H0.price):   # LL and LH
            return None
        return WaveStructure("short", H0.price, H0.idx, L1.price, L1.idx, H1.price, H1.idx)

    return None
