"""Delta divergence: price breaks prior-window high/low but delta fails to confirm.

Bearish: price > window max close, delta < window max delta  (buyers exhausting)
Bullish: price < window min close, delta > window min delta  (sellers exhausting)

Used as a hard gate in direction_engine — bearish divergence vetoes long entry,
bullish divergence vetoes short entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


@dataclass(frozen=True)
class DeltaDivergence:
    fired: bool
    direction: Literal["bearish", "bullish", "none"]
    price_vs_window: float   # how far price broke beyond window extreme
    delta_vs_window: float   # how far delta lagged behind window extreme


_EMPTY = DeltaDivergence(fired=False, direction="none",
                         price_vs_window=0.0, delta_vs_window=0.0)


def detect(bar: Bar, history: list[Bar], window: int = 5) -> DeltaDivergence:
    """Detect delta divergence on current bar vs prior `window` bars.

    Requires `len(history) >= window`. Returns empty result if insufficient history.
    """
    if len(history) < window:
        return _EMPTY
    prior = history[-window:]
    prior_closes = [b.ohlc.c for b in prior]
    prior_deltas = [b.delta or 0.0 for b in prior]
    cur_close = bar.ohlc.c
    cur_delta = bar.delta or 0.0

    max_close = max(prior_closes)
    min_close = min(prior_closes)
    max_delta = max(prior_deltas)
    min_delta = min(prior_deltas)

    if cur_close > max_close and cur_delta < max_delta:
        return DeltaDivergence(
            fired=True,
            direction="bearish",
            price_vs_window=round(cur_close - max_close, 4),
            delta_vs_window=round(max_delta - cur_delta, 4),
        )
    if cur_close < min_close and cur_delta > min_delta:
        return DeltaDivergence(
            fired=True,
            direction="bullish",
            price_vs_window=round(min_close - cur_close, 4),
            delta_vs_window=round(cur_delta - min_delta, 4),
        )
    return _EMPTY


def from_store(symbol: str, tf: str, window: int = 5) -> DeltaDivergence:
    """Convenience: pull recent bars from state_store and run detect()."""
    try:
        from pipeline.state_store import store
        bars = store().recent(symbol, tf, window + 1)
        if len(bars) < window + 1:
            return _EMPTY
        return detect(bars[-1], bars[:-1], window=window)
    except Exception:
        return _EMPTY
