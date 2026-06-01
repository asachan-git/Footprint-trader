"""Unfinished auction: single-print at bar extreme.

A bar extreme is "unfinished" when only one side of the market traded at the
top/bottom cells — the auction never found two-sided participation. Market
tends to return to test these levels.

  unfinished_up  : top cells have only ask_vol (aggressive buyers, no sellers)
                   → price drawn back up to find sellers → blocks SHORT entry
  unfinished_down: bottom cells have only bid_vol (aggressive sellers, no buyers)
                   → price drawn back down to find buyers → blocks LONG entry

Detection looks at the top/bottom `n_cells` with meaningful volume (≥ min_vol).
Requires a minimum imbalance ratio (default 4×) to confirm single-print.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..footprint import FootprintMatrix


@dataclass(frozen=True)
class UnfinishedAuction:
    fired: bool
    side: Literal["up", "down", "none"]   # up = extreme high unfinished, down = extreme low
    extreme_price: float
    ask_vol: float
    bid_vol: float
    ratio: float   # dominant / recessive side ratio


_EMPTY = UnfinishedAuction(fired=False, side="none",
                            extreme_price=0.0, ask_vol=0.0, bid_vol=0.0, ratio=0.0)


def detect(fp: FootprintMatrix, n_cells: int = 2, min_vol: float = 0.5,
           min_ratio: float = 4.0) -> UnfinishedAuction:
    """Detect unfinished auction at bar extremes.

    Args:
        n_cells:   Number of price cells from extreme to examine.
        min_vol:   Minimum total volume at extreme to fire (avoids zero-volume ghost cells).
        min_ratio: ask/bid (or bid/ask) ratio required to call it a single-print.
    """
    cells = fp.cells
    if not cells:
        return _EMPTY

    # Find top-N meaningful cells (highest prices with total vol >= min_vol)
    top_cells = [c for c in reversed(cells) if (c.ask_vol + c.bid_vol) >= min_vol][:n_cells]
    # Find bottom-N meaningful cells (lowest prices with total vol >= min_vol)
    bot_cells = [c for c in cells if (c.ask_vol + c.bid_vol) >= min_vol][:n_cells]

    if top_cells:
        ask_sum = sum(c.ask_vol for c in top_cells)
        bid_sum = sum(c.bid_vol for c in top_cells)
        if ask_sum > 0 and bid_sum < ask_sum / min_ratio:
            # Aggressive buyers at top, essentially no sellers → unfinished_up
            return UnfinishedAuction(
                fired=True, side="up",
                extreme_price=top_cells[0].price,
                ask_vol=round(ask_sum, 2),
                bid_vol=round(bid_sum, 2),
                ratio=round(ask_sum / max(bid_sum, 1e-9), 2),
            )

    if bot_cells:
        ask_sum = sum(c.ask_vol for c in bot_cells)
        bid_sum = sum(c.bid_vol for c in bot_cells)
        if bid_sum > 0 and ask_sum < bid_sum / min_ratio:
            # Aggressive sellers at bottom, essentially no buyers → unfinished_down
            return UnfinishedAuction(
                fired=True, side="down",
                extreme_price=bot_cells[0].price,
                ask_vol=round(ask_sum, 2),
                bid_vol=round(bid_sum, 2),
                ratio=round(bid_sum / max(ask_sum, 1e-9), 2),
            )

    return _EMPTY


def from_store(symbol: str, tf: str, **kwargs) -> UnfinishedAuction:
    """Convenience: pull latest bar from state_store and run detect()."""
    try:
        from pipeline.state_store import store
        from pipeline.footprint import build as build_fp
        bars = store().recent(symbol, tf, 1)
        if not bars:
            return _EMPTY
        return detect(build_fp(bars[-1]), **kwargs)
    except Exception:
        return _EMPTY
