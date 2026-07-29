"""Cycle value function — deterministic trail/kill prices from a grid cycle's book.

Replaces fixed-USD exit knobs (bias_trail_activate_usd, cycle_net_target_usd) with
closed-form outputs computed from the cycle's actual order book: open legs and
pending legs with (side, price, qty), per-side HVN TPs as inputs, plus realized PnL.

The cycle's projected PnL is piecewise-linear in price. Along a monotone path from
the current mid, the slope changes only at kinks:

  * a pending leg's trigger — touching it fills the leg (stop or limit, both
    trigger on touch given they sit on the path side), slope grows;
  * an open leg's TP (or an opposite leg's SL) — crossing it converts that leg to
    realized, slope drops.

Every output is therefore exact per segment — no iteration, no tolerance:

  breakeven(book, direction)          first price where projected PnL >= 0
  trail_activation(book, d, F, dir)   first price from which an immediate reversal
                                      of d still locks >= F on CLOSE_ALL
  kill_price(book, L, direction)      first adverse price where projected PnL <= -L,
                                      or None when the opposite ladder hedges the
                                      move first (loss is then bounded)
  worst_case_loss(book)               infimum of projected PnL over both directions

Model assumptions (kept deliberately conservative and documented here once):

  * Spread cost is charged once per pending fill (spread × qty × contract) —
    a round-trip approximation at entry. Exits close at the projected price.
  * trail_activation assumes the trail, once armed, cancels adverse-side pendings
    (the integration does this), so the d-retrace is a straight CLOSE_ALL at
    p - d with the open set as of p: no re-fills on the way back.
  * PnL units are `contract`-scaled: pass contract_size (e.g. 100 for XAU) for
    account-currency, or 1 to match backtest/fill_engine gross price-units × lot.

Pure module: stdlib only, no execution-state imports. The adapters that build a
CycleBook from live aggregates or the backtest Broker live in cycle_book.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

UP = "up"
DOWN = "down"

_EPS = 1e-12


@dataclass(frozen=True)
class BookLeg:
    side: str            # "buy" | "sell"
    price: float         # entry (filled) or trigger (pending)
    qty: float           # lots
    filled: bool
    tp: float = 0.0      # 0 = none; HVN-derived, an input to this module
    sl: float = 0.0      # 0 = none


@dataclass(frozen=True)
class CycleBook:
    legs: tuple[BookLeg, ...]
    realized: float      # PnL already banked this cycle (same units as outputs)
    contract: float      # units/lot multiplier (1.0 for gross price-units × lot)
    spread: float        # price-units cost charged per pending fill
    mid: float
    step: float
    fulcrum: float


# ── internal: mirror so every walk is an "up" walk ───────────────────────────

def _mirror(book: CycleBook) -> CycleBook:
    """Reflect prices about 0 and swap sides; a down-walk becomes an up-walk."""
    def flip(leg: BookLeg) -> BookLeg:
        return replace(
            leg,
            side="sell" if leg.side == "buy" else "buy",
            price=-leg.price,
            tp=-leg.tp if leg.tp else 0.0,
            sl=-leg.sl if leg.sl else 0.0,
        )
    return replace(
        book,
        legs=tuple(flip(l) for l in book.legs),
        mid=-book.mid,
        fulcrum=-book.fulcrum,
        step=book.step,
    )


def _oriented(book: CycleBook, direction: str) -> CycleBook:
    if direction == UP:
        return book
    if direction == DOWN:
        return _mirror(book)
    raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")


def _signed(leg: BookLeg) -> float:
    return leg.qty if leg.side == "buy" else -leg.qty


def _segments(book: CycleBook):
    """Yield (lo, hi, pnl_lo, slope) along the up path from book.mid to +inf.

    pnl within a segment: pnl_lo + slope × (p - lo). The last segment has
    hi = +inf. Events exactly at mid are treated as already behind the path.
    """
    mid = book.mid
    c = book.contract

    # (trigger_price, order, leg, kind) — fills before closes at the same price:
    # a leg cannot fill and immediately count its TP at the identical price.
    events: list[tuple[float, int, BookLeg, str]] = []
    # NB: tp/sl use 0.0 as the "none" sentinel; in the mirrored frame real levels
    # are negative, so the sentinel must be tested by truthiness, never by > mid.
    for leg in book.legs:
        if leg.filled:
            if leg.side == "buy" and leg.tp and leg.tp > mid:
                events.append((leg.tp, 1, leg, "close_tp"))
            elif leg.side == "sell" and leg.sl and leg.sl > mid:
                events.append((leg.sl, 1, leg, "close_sl"))
        else:
            if leg.price > mid:
                events.append((leg.price, 0, leg, "fill"))
                if leg.side == "buy" and leg.tp and leg.tp > leg.price:
                    events.append((leg.tp, 1, leg, "close_tp"))
                elif leg.side == "sell" and leg.sl and leg.sl > leg.price:
                    events.append((leg.sl, 1, leg, "close_sl"))
    events.sort(key=lambda e: (e[0], e[1]))

    open_legs: dict[int, BookLeg] = {
        id(l): l for l in book.legs if l.filled
    }
    entry_of: dict[int, float] = {id(l): l.price for l in book.legs if l.filled}
    realized = book.realized
    lo = mid
    pnl_lo = realized + sum(
        c * _signed(l) * (mid - entry_of[k]) for k, l in open_legs.items()
    )

    for px, _order, leg, kind in events:
        slope = c * sum(_signed(l) for l in open_legs.values())
        if px > lo + _EPS:
            yield lo, px, pnl_lo, slope
            pnl_lo = pnl_lo + slope * (px - lo)
            lo = px
        key = id(leg)
        if kind == "fill":
            open_legs[key] = leg
            entry_of[key] = leg.price
            realized -= book.spread * leg.qty * c
            pnl_lo -= book.spread * leg.qty * c
        else:  # close_tp / close_sl — only if the leg is still open on this path
            if key in open_legs:
                del open_legs[key]
                del entry_of[key]
                # closing at px: its mark is already inside pnl_lo; realizing at
                # the same price changes nothing at the kink, only the slope after.

    slope = c * sum(_signed(l) for l in open_legs.values())
    yield lo, math.inf, pnl_lo, slope


# ── public API ────────────────────────────────────────────────────────────────

def pnl_at(book: CycleBook, p: float) -> float:
    """Projected cycle PnL if price moves monotonically from mid to p."""
    if p >= book.mid:
        b, target = book, p
    else:
        b, target = _mirror(book), -p
    last = book.realized
    for lo, hi, pnl_lo, slope in _segments(b):
        if target <= hi + _EPS:
            return pnl_lo + slope * (target - lo)
        last = pnl_lo + slope * (hi - lo)
    return last  # unreachable — final segment is unbounded


def breakeven(book: CycleBook, direction: str) -> float | None:
    """Smallest price along `direction` where projected PnL >= 0. None if never."""
    b = _oriented(book, direction)
    for lo, hi, pnl_lo, slope in _segments(b):
        if pnl_lo >= 0:
            return lo if direction == UP else -lo
        if slope > _EPS:
            p = lo + (-pnl_lo) / slope
            if p <= hi:
                return p if direction == UP else -p
    return None


def trail_activation(book: CycleBook, d: float, F: float,
                     direction: str) -> float | None:
    """First price from which a d-retrace CLOSE_ALL still locks >= F.

    Locked value at path price p: realized-so-far along the path plus every
    open leg marked to the exit price p - d (no re-fills on the retrace — the
    trail cancels adverse pendings at activation). None when no price achieves
    the floor (hedged or adverse book: locked slope <= 0 on every segment).
    """
    if d < 0:
        raise ValueError("trail distance d must be >= 0")
    b = _oriented(book, direction)
    c = b.contract

    # Rebuild open-set net qty per segment from the slope: slope = c × net_qty.
    for lo, hi, pnl_lo, slope in _segments(b):
        # locked(p) = pnl(p) - slope × d  (marking the same open set d lower)
        locked_lo = pnl_lo - slope * d
        if locked_lo >= F:
            return lo if direction == UP else -lo
        if slope > _EPS:
            p = lo + (F - locked_lo) / slope
            if p <= hi:
                return p if direction == UP else -p
    return None


def kill_price(book: CycleBook, L: float, direction: str) -> tuple[float | None, float]:
    """(first price along `direction` where projected PnL <= -L, worst PnL).

    Walks the adverse path including hedge fills (opposite stops flatten the
    slope). Returns (None, bound) when the book hedges before -L is reached and
    the loss stays bounded; worst is -inf when the terminal slope keeps losing.
    """
    if L < 0:
        raise ValueError("max loss L must be >= 0")
    b = _oriented(book, direction)
    worst = math.inf
    for lo, hi, pnl_lo, slope in _segments(b):
        if pnl_lo <= -L:
            return (lo if direction == UP else -lo), min(worst, pnl_lo)
        worst = min(worst, pnl_lo)
        if slope < -_EPS:
            p = lo + (-L - pnl_lo) / slope
            if p <= hi:
                return (p if direction == UP else -p), -L
            if math.isinf(hi):
                worst = -math.inf
        elif math.isinf(hi):
            worst = min(worst, pnl_lo)
    return None, worst


def worst_case_loss(book: CycleBook) -> float:
    """Infimum of projected PnL over both directions (-inf if unbounded)."""
    _, w_up = kill_price(book, math.inf, UP)
    _, w_dn = kill_price(book, math.inf, DOWN)
    return min(w_up, w_dn)
