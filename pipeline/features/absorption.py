"""Absorption: high volume at a price level with little/no price progression past it.

Canonical definition: delta at bar extreme is OPPOSITE to bar direction.
Bear candle + buy-vol dominant in upper 10% of range = buyers absorbed at high = bearish.
Bull candle + sell-vol dominant in lower 10% of range = sellers absorbed at low = bullish.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..footprint import FootprintMatrix
from ..types import Bar


@dataclass(frozen=True)
class Absorption:
    price: float
    side: str          # "buy" (absorbed at low) | "sell" (absorbed at high)
    volume: int
    bar_pct: float
    is_wick_trap: bool = False   # wick >= 30% of bar range with delta confirming opposite


def detect_absorption(
    bar: Bar,
    fp: FootprintMatrix,
    absorb_ratio: float = 0.30,
    wick_tolerance: float = 0.0,
) -> tuple[Absorption, ...]:
    if not fp.cells:
        return ()
    total = sum(c.total for c in fp.cells)
    if total == 0:
        return ()
    out: list[Absorption] = []
    high = bar.ohlc.h
    low = bar.ohlc.l
    total_range = high - low

    for c in fp.cells:
        pct = c.total / total
        if pct < absorb_ratio:
            continue
        if abs(c.price - low) <= wick_tolerance:
            wick_pct = (min(bar.ohlc.o, bar.ohlc.c) - low) / total_range if total_range > 0 else 0.0
            out.append(Absorption(
                price=c.price, side="buy", volume=c.total, bar_pct=pct,
                is_wick_trap=wick_pct >= 0.30,
            ))
        elif abs(c.price - high) <= wick_tolerance:
            wick_pct = (high - max(bar.ohlc.o, bar.ohlc.c)) / total_range if total_range > 0 else 0.0
            out.append(Absorption(
                price=c.price, side="sell", volume=c.total, bar_pct=pct,
                is_wick_trap=wick_pct >= 0.30,
            ))
    return tuple(out)


def detect_close_failure_absorption(
    bar: Bar,
    fp: FootprintMatrix,
    min_share: float = 0.20,
) -> tuple[Absorption, ...]:
    """Absorption inferred from close failing to clear the heavy-volume node.

    SELL absorption: highest-volume ASK price (buy aggressor pile-up) sits
        above the bar close → buyers pushed, got absorbed → bearish.
    BUY absorption: highest-volume BID price (sell aggressor pile-up) sits
        below the bar close → sellers pushed, got absorbed → bullish.

    `min_share` filters out trivial peaks: the heavy node must hold at least
    this fraction of total bar volume.
    """
    if not bar.bid_ladder or not bar.ask_ladder:
        return ()
    total_bid = sum(lvl.vol for lvl in bar.bid_ladder)
    total_ask = sum(lvl.vol for lvl in bar.ask_ladder)
    total = total_bid + total_ask
    if total <= 0:
        return ()
    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    total_range = max(h - l, 1e-9)

    out: list[Absorption] = []

    # SELL absorption: pick the ask-ladder level (buy aggressor) with max vol.
    top_ask = max(bar.ask_ladder, key=lambda lvl: lvl.vol)
    if top_ask.vol / total >= min_share and c < top_ask.price:
        wick_pct = (h - max(o, c)) / total_range
        out.append(Absorption(
            price=top_ask.price, side="sell",
            volume=int(top_ask.vol), bar_pct=top_ask.vol / total,
            is_wick_trap=wick_pct >= 0.30,
        ))

    # BUY absorption: pick bid-ladder (sell aggressor) level with max vol.
    top_bid = max(bar.bid_ladder, key=lambda lvl: lvl.vol)
    if top_bid.vol / total >= min_share and c > top_bid.price:
        wick_pct = (min(o, c) - l) / total_range
        out.append(Absorption(
            price=top_bid.price, side="buy",
            volume=int(top_bid.vol), bar_pct=top_bid.vol / total,
            is_wick_trap=wick_pct >= 0.30,
        ))

    return tuple(out)


def detect_canonical_absorption(bar: Bar, fp: FootprintMatrix) -> tuple[Absorption, ...]:
    """Canonical absorption: delta at bar extreme is OPPOSITE to bar direction.

    Bear candle (close < open): buy-vol dominant in upper 10% = bearish signal (buyers absorbed).
    Bull candle (close > open): sell-vol dominant in lower 10% = bullish signal (sellers absorbed).
    """
    if not bar.bid_ladder or not bar.ask_ladder:
        return ()

    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    total_range = h - l
    if total_range <= 0:
        return ()

    is_bear = c < o
    is_bull = c > o
    extreme_zone = total_range * 0.10

    results: list[Absorption] = []

    if is_bear:
        upper_threshold = h - extreme_zone
        upper_bid = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price >= upper_threshold)
        upper_ask = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price >= upper_threshold)
        upper_total = upper_bid + upper_ask
        if upper_total > 0 and upper_ask > 0 and upper_bid / upper_ask >= 1.5:
            wick_pct = (h - max(o, c)) / total_range
            results.append(Absorption(
                price=h, side="sell", volume=int(upper_total), bar_pct=upper_bid / upper_total,
                is_wick_trap=wick_pct >= 0.30,
            ))

    if is_bull:
        lower_threshold = l + extreme_zone
        lower_ask = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price <= lower_threshold)
        lower_bid = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price <= lower_threshold)
        lower_total = lower_bid + lower_ask
        if lower_total > 0 and lower_bid > 0 and lower_ask / lower_bid >= 1.5:
            wick_pct = (min(o, c) - l) / total_range
            results.append(Absorption(
                price=l, side="buy", volume=int(lower_total), bar_pct=lower_ask / lower_total,
                is_wick_trap=wick_pct >= 0.30,
            ))

    return tuple(results)
