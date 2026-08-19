"""ATR — Average True Range across N closed bars.

True Range per bar = max(h-l, |h-prev_c|, |l-prev_c|).
ATR = simple mean of TR over last N bars (Wilder's smoothing optional later).

Used by grid_placer to compute step size (0.5 × ATR_15m default).
"""

from __future__ import annotations

from pipeline.types import Bar


def atr(bars: list[Bar], period: int = 14) -> float:
    """Compute ATR over the last `period` bars. Returns 0.0 if insufficient data."""
    if len(bars) < 2:
        return 0.0
    n = min(period, len(bars) - 1)
    trs: list[float] = []
    for i in range(len(bars) - n, len(bars)):
        cur = bars[i].ohlc
        prev = bars[i - 1].ohlc
        tr = max(
            cur.h - cur.l,
            abs(cur.h - prev.c),
            abs(cur.l - prev.c),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs) / len(trs)


def atr_from_store(symbol: str, tf: str, period: int = 14) -> float:
    """Convenience: fetch last `period+1` bars from state_store and compute ATR.

    Drops the sentinel/forming bar (close_ts >= 9_000_000_000). Such a bar carries a
    STALE price and poisons the true-range with a huge phantom gap — observed
    2026-07-15: 1m ATR read 7.85 against a true ~0.8 because a sentinel sat at 4130
    while price was 4034, a 96pt fake range. ATR sizes the grid step, so that became
    an oversized ladder whose TP fell INSIDE it, and legs went out with tp=0.

    state_store.put() now refuses to persist such a bar at all, but this stays as the
    read-side guard: bars already in a pre-fix file, or arriving by another path, must
    not reach the sizing math. Same filter maybe_emit uses. Fetches a few extra bars so
    `period` real ones survive the drop."""
    from pipeline.state_store import store
    bars = [b for b in store().recent(symbol, tf, period + 4)
            if getattr(b, "close_ts", 0) and b.close_ts < 9_000_000_000]
    return atr(bars[-(period + 1):], period=period)
