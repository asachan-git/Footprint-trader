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


def detect_canonical_absorption(bar: Bar, fp: FootprintMatrix,
                                mode: str = "momentum") -> tuple[Absorption, ...]:
    """Absorption at a bar extreme. Convention: ask_vol = aggressive BUYs,
    bid_vol = aggressive SELLs (see bybit/footprint_builder.py).

    Two selection modes (the ratio threshold and returned side are unchanged;
    only WHICH cells must dominate at the extreme differs):

      momentum (default, legacy behaviour) — the *winning* side already dominates
        the extreme: bear candle with bid(sell) dominant in upper 10%, or bull
        candle with ask(buy) dominant in lower 10%. This is really a
        one-sided/continuation candle, NOT textbook absorption, but it is what
        coup has traded historically — kept as default until the A/B proves out.

      canonical — true absorption: the *aggressor* side pushes into the extreme
        and is absorbed (rejected). Bear candle with ask(buy) dominant in the
        upper 10% = buyers pushed the high and failed → buyers absorbed → short.
        Bull candle with bid(sell) dominant in the lower 10% = sellers pushed the
        low and failed → sellers absorbed → long.

    side encodes the WINNER ("sell" → short, "buy" → long) in both modes.
    """
    if not bar.bid_ladder or not bar.ask_ladder:
        return ()

    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    total_range = h - l
    if total_range <= 0:
        return ()

    canonical = mode == "canonical"
    is_bear = c < o
    is_bull = c > o
    extreme_zone = total_range * 0.10

    results: list[Absorption] = []

    # ── reversal mode (user's observed pattern: absorption→aggression→cont) ──
    # TWO ZONES per the user's spec. LONG (close up = sellers trapped):
    #   ZONE A (extreme low, bottom 10%): aggressive SELLERS (bid) dominant —
    #     the side that got TRAPPED (heavy selling at the low that price left).
    #   ZONE B (small band JUST BELOW the close): aggressive BUYERS (ask)
    #     dominant — the side that LED the recovery into the close.
    # "Trigger can be weak" → only dominance (≥) is required, not heavy ratios;
    # the decisive aggression-with-result is the NEXT candle (coup._confirm).
    # SHORT = mirror at the high (buyers trapped at top, sellers lead below close
    # = above the close band). is_wick_trap kept for info only.
    if mode == "reversal":
        BAND = total_range * 0.12       # near-close band (the LEADING side)
        bar_total = (fp.total_bid + fp.total_ask) if fp else 0.0
        if bar_total <= 0:
            return ()
        if is_bull:
            lo_t = l + extreme_zone
            lo_bid = sum(v.vol for v in bar.bid_ladder if v.price <= lo_t)   # trapped sellers
            lo_ask = sum(v.vol for v in bar.ask_ladder if v.price <= lo_t)
            nc_lo = c - BAND
            nc_ask = sum(v.vol for v in bar.ask_ladder if nc_lo <= v.price <= c)  # leading buyers
            nc_bid = sum(v.vol for v in bar.bid_ladder if nc_lo <= v.price <= c)
            trapped_sellers = lo_bid > 0 and lo_bid >= lo_ask
            leading_buyers  = nc_ask > 0 and nc_ask >= nc_bid
            if trapped_sellers and leading_buyers:
                results.append(Absorption(
                    price=l, side="buy", volume=int(lo_bid + lo_ask),
                    bar_pct=nc_ask / bar_total,
                    is_wick_trap=((min(o, c) - l) / total_range) >= 0.30,
                ))
        if is_bear:
            up_t = h - extreme_zone
            up_ask = sum(v.vol for v in bar.ask_ladder if v.price >= up_t)   # trapped buyers
            up_bid = sum(v.vol for v in bar.bid_ladder if v.price >= up_t)
            nc_hi = c + BAND
            na_bid = sum(v.vol for v in bar.bid_ladder if c <= v.price <= nc_hi)  # leading sellers
            na_ask = sum(v.vol for v in bar.ask_ladder if c <= v.price <= nc_hi)
            trapped_buyers  = up_ask > 0 and up_ask >= up_bid
            leading_sellers = na_bid > 0 and na_bid >= na_ask
            if trapped_buyers and leading_sellers:
                results.append(Absorption(
                    price=h, side="sell", volume=int(up_ask + up_bid),
                    bar_pct=na_bid / bar_total,
                    is_wick_trap=((h - max(o, c)) / total_range) >= 0.30,
                ))
        return tuple(results)

    if is_bear:
        upper_threshold = h - extreme_zone
        upper_bid = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price >= upper_threshold)
        upper_ask = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price >= upper_threshold)
        upper_total = upper_bid + upper_ask
        # canonical: buyers (ask) pushed the high and were absorbed → ask must dominate
        # momentum: sellers (bid) already dominant at the high
        num, den = (upper_ask, upper_bid) if canonical else (upper_bid, upper_ask)
        dom = upper_ask if canonical else upper_bid
        if upper_total > 0 and den > 0 and num / den >= 1.5:
            wick_pct = (h - max(o, c)) / total_range
            results.append(Absorption(
                price=h, side="sell", volume=int(upper_total), bar_pct=dom / upper_total,
                is_wick_trap=wick_pct >= 0.30,
            ))

    if is_bull:
        lower_threshold = l + extreme_zone
        lower_ask = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price <= lower_threshold)
        lower_bid = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price <= lower_threshold)
        lower_total = lower_bid + lower_ask
        # canonical: sellers (bid) pushed the low and were absorbed → bid must dominate
        # momentum: buyers (ask) already dominant at the low
        num, den = (lower_bid, lower_ask) if canonical else (lower_ask, lower_bid)
        dom = lower_bid if canonical else lower_ask
        if lower_total > 0 and den > 0 and num / den >= 1.5:
            wick_pct = (min(o, c) - l) / total_range
            results.append(Absorption(
                price=l, side="buy", volume=int(lower_total), bar_pct=dom / lower_total,
                is_wick_trap=wick_pct >= 0.30,
            ))

    return tuple(results)
