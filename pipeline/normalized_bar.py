"""Scale-free bar representation — all price fields normalized as %-offset from close.

Used by analysis modules and grid_placer to produce signals that are
instrument-agnostic. A NormalizedBar with `h_pct=+0.3%` says "high was
0.3% above close" regardless of whether the asset trades at $30k BTC
or $4500 XAU.

Conversion is lossless when paired with the absolute `close` price —
all other fields can be recovered: high = close × (1 + h_pct).

Used in:
  - execution/grid_placer.py — outputs NormalizedGridPlan (legs as pct offsets)
  - execution/venue_translator.py — translates back to absolute prices
    using the execution venue's live quote (not the analysis venue's price)
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.types import Bar


@dataclass(frozen=True)
class NormalizedBar:
    bar_id: str
    symbol: str          # analysis-venue symbol (e.g. BTCUSDT on Bybit)
    tf: str
    close_ts: int

    # Anchor
    close: float         # absolute close price (the only abs field)

    # Scale-free offsets (all expressed as fraction of close)
    o_pct: float         # (open - close) / close
    h_pct: float         # (high - close) / close
    l_pct: float         # (low - close) / close
    range_pct: float     # (high - low) / close
    body_pct: float      # |close - open| / close
    upper_wick_pct: float
    lower_wick_pct: float

    # Carried orderflow (already venue-neutral)
    delta: float | None
    poc: float | None    # absolute; convert via poc_pct = (poc - close) / close on demand


def from_bar(bar: Bar) -> NormalizedBar:
    """Convert a raw Bar into NormalizedBar (scale-free)."""
    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    if c <= 0:
        raise ValueError(f"close must be > 0, got {c}")
    body_high = max(o, c)
    body_low = min(o, c)
    return NormalizedBar(
        bar_id=bar.bar_id, symbol=bar.symbol, tf=bar.tf, close_ts=bar.close_ts,
        close=c,
        o_pct=(o - c) / c,
        h_pct=(h - c) / c,
        l_pct=(l - c) / c,
        range_pct=(h - l) / c,
        body_pct=abs(c - o) / c,
        upper_wick_pct=(h - body_high) / c,
        lower_wick_pct=(body_low - l) / c,
        delta=bar.delta,
        poc=bar.poc,
    )


def pct_offset(price: float, anchor: float) -> float:
    """Return (price - anchor) / anchor — sign matches direction from anchor."""
    if anchor <= 0:
        return 0.0
    return (price - anchor) / anchor


def apply_pct_offset(anchor: float, pct: float) -> float:
    """Return anchor × (1 + pct). Use to translate %-offset → absolute on any venue."""
    return anchor * (1.0 + pct)
