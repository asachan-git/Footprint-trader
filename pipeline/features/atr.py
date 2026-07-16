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


def atr_from_store(symbol: str, tf: str, period: int = 14, _guard: bool = True) -> float:
    """Convenience: fetch last `period+1` bars from state_store and compute ATR.

    Drops the sentinel/forming bar (close_ts >= 9_000_000_000, e.g. the ts=9999999999
    frozen bar carrying a STALE price) — it poisons the true-range with a huge phantom
    gap (observed 2026-07-15: 1m ATR read 7.85 vs true ~0.8 because a sentinel bar sat at
    4130 while price was 4034, a 96pt fake range → oversized 1m grid step → the TP fell
    inside the ladder and legs placed with tp=0). Same filter maybe_emit uses. Fetch a few
    extra bars so `period` real bars survive the drop.

    PLAUSIBILITY CLAMP (2026-07-15): the sentinel guard above + state_store.put() block the
    *specific* ts=9999999999 fixture, but ANY future bad bar (fat-finger tick, feed glitch)
    producing a phantom range would inflate the shorter-TF ATR the same way. A physical
    invariant catches the whole class: a 1m true-range CANNOT exceed the 5m/15m range that
    contains it, so 1m ATR must be < higher-TF ATR. If 1m ATR reads implausibly high vs 5m,
    clamp it to the 5m value (a safe in-family number → sane grid step) and log. `_guard=False`
    on the inner 5m call prevents infinite recursion."""
    from pipeline.state_store import store
    bars = [b for b in store().recent(symbol, tf, period + 4)
            if getattr(b, "close_ts", 0) and b.close_ts < 9_000_000_000]
    val = atr(bars[-(period + 1):], period=period)
    if _guard and tf == "1m" and val > 0:
        ref = atr_from_store(symbol, "5m", period, _guard=False)
        if ref > 0 and val > 1.5 * ref:   # 1m ATR physically < 5m ATR; >1.5× = phantom range
            import logging
            logging.getLogger("atr").warning(
                "[atr_implausible] %s 1m ATR %.4f > 1.5x 5m %.4f — clamping to 5m "
                "(phantom range from a bad bar?)", symbol, val, ref)
            return ref
    return val
