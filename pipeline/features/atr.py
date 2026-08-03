"""ATR — Average True Range across N closed bars.

True Range per bar = max(h-l, |h-prev_c|, |l-prev_c|).
ATR = simple mean of TR over last N bars (Wilder's smoothing optional later).

Used by grid_placer to compute step size (0.5 × ATR_15m default).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline.types import Bar

# Symbols that trade a real 24/7 market — their weekend bars carry genuine range and
# must NOT be skipped. Everything else is treated as session-anchored (gold, and any
# future non-crypto instrument): no real trading happens Sat/Sun, so weekend bars are
# thin synthetic drift, not signal. Mirrors pipeline.features.vp_cache's weekend-skip
# rule (_prev_trading_day_key) — same underlying bug, different code path.
_ALWAYS_24_7 = {"BTCUSDT", "BTCUSD"}


def _is_weekend_bar(b: Bar) -> bool:
    ts = getattr(b, "close_ts", 0) or 0
    if not ts or ts >= 9_000_000_000:
        return False
    return datetime.fromtimestamp(ts, timezone.utc).weekday() >= 5   # 5=Sat, 6=Sun


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

    Drops the sentinel/forming bar (close_ts >= 9_000_000_000, e.g. the ts=9999999999
    frozen bar carrying a STALE price) — it poisons the true-range with a huge phantom
    gap (observed 2026-07-15: 1m ATR read 7.85 vs true ~0.8 because a sentinel bar sat at
    4130 while price was 4034, a 96pt fake range → oversized 1m grid step → the TP fell
    inside the ladder and legs placed with tp=0). Same filter maybe_emit uses.

    Drops weekend bars for non-24/7 symbols (observed 2026-08-03: gold's Monday-morning
    ATR(14) window was entirely Sunday 06:30-07:30 bars — thin Binance-XAUT pre-market
    drift, ranges 0.4-3.4 vs a normal 15m range of 7+ — because the store just returns
    the newest N bars with no session awareness, and nothing fresher existed across the
    ~20h weekend gap. Gave ATR~0.72 -> step~0.36, a grid roughly 10x tighter than
    intended. Same underlying bug 255bcbd (2026-06-29) fixed for the VP/session-history
    path; that fix never covered this one). Walks back further to find `period` real
    (non-weekend) bars instead of taking the literal newest N."""
    from pipeline.state_store import store
    fetch_n = period + 4
    bars: list[Bar] = []
    skip_weekend = symbol.upper() not in _ALWAYS_24_7
    # Escalate the fetch window until enough non-weekend bars survive, capped so a
    # symbol/TF with genuinely sparse history doesn't spin forever.
    for _ in range(6):
        raw = store().recent(symbol, tf, fetch_n)
        bars = [b for b in raw if getattr(b, "close_ts", 0) and b.close_ts < 9_000_000_000
                and not (skip_weekend and _is_weekend_bar(b))]
        if len(bars) >= period + 1 or len(raw) < fetch_n:
            break
        fetch_n *= 4
    return atr(bars[-(period + 1):], period=period)
