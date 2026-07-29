"""Adapters that build a cycle_value.CycleBook from each execution path.

Two sources, one shape:

  * book_from_broker — backtest/paper: the fill_engine.Broker holds true
    per-order rows, so the book is exact. Gross price-units × lot (contract=1)
    to match the Broker's own PnL convention.
  * book_from_arm — live shape: the EA poll carries per-magic AGGREGATES only
    (counts + lot sums), so the book is RECONSTRUCTED from the arm-time ladder
    geometry persisted in the cycle dict (book_buy_legs / book_sell_legs).
    Stop ladders fill innermost-out and the side-level HVN TP closes a whole
    side at once, so leg identity is deterministic — until something else
    closes legs (CLOSE_SIDE frac, leg_closed_other, manual). Every such case
    must be caught by the cross-checks here and reported as diverged=True so
    the caller can fall back to legacy exits.

The reconstruction gate in backtest/fidelity_check.py proves the two adapters
agree on every simulated poll — the only offline evidence the live adapter is
trustworthy.
"""
from __future__ import annotations

from execution.cycle_value import BookLeg, CycleBook

_LOTS_TOL_DEFAULT = 0.005


# ── backtest path: exact ──────────────────────────────────────────────────────

def book_from_broker(broker, magic: int, mid: float, step: float, fulcrum: float,
                     armed_ts: int = 0, contract: float = 1.0,
                     spread: float = 0.0) -> CycleBook:
    """Exact book for one magic from a fill_engine.Broker.

    realized = closed-leg PnL for this magic since armed_ts, from the Broker's
    event log (tp/sl/close_all/close_side events carry pnl).
    """
    legs: list[BookLeg] = []
    for p in broker.positions:
        if p.magic != magic:
            continue
        legs.append(BookLeg(side="buy" if p.is_buy else "sell", price=p.entry,
                            qty=p.lot, filled=True, tp=p.tp, sl=p.sl))
    for p in broker.pendings:
        if p.magic != magic:
            continue
        legs.append(BookLeg(side="buy" if p.is_buy else "sell", price=p.price,
                            qty=p.lot, filled=False, tp=p.tp, sl=p.sl))
    realized = sum(
        e.pnl for e in broker.events
        if e.magic == magic and e.ts >= armed_ts
        and e.kind in ("tp", "sl", "close_all", "close_side")
    )
    return CycleBook(legs=tuple(legs), realized=realized, contract=contract,
                     spread=spread, mid=mid, step=step, fulcrum=fulcrum)


# ── live shape: reconstructed from aggregates ─────────────────────────────────

def book_from_arm(cyc: dict, row: dict, mid: float, contract: float,
                  spread: float = 0.0,
                  lots_tol: float = _LOTS_TOL_DEFAULT,
                  pnl_tol: float = 0.0) -> tuple[CycleBook, bool, list[str]]:
    """Reconstruct the book from the arm-time ladder + a per-magic poll row.

    cyc  — the arm-state dict; must carry book_buy_legs / book_sell_legs
           ([[price, lot], ...] as enqueued), tp_up / tp_dn, step, fulcrum,
           max_buys_seen / max_sells_seen.
    row  — the EA magics row: buys, sells, buy_lots, sell_lots,
           buy_pendings, sell_pendings.

    Returns (book, ok, notes). ok=False means the aggregates cannot be explained
    by innermost-first fills + whole-side TP closes — the caller must treat the
    cycle as cv_diverged and use legacy exits.

    Realized PnL: aggregates cannot distinguish a TP-flat side from an SL-flat
    one (MOVE_BE runners close at breakeven, trail SLs and CLOSE_ALL at market),
    so closures are NOT priceable from geometry — G-RECON proved pricing them at
    the side TP mis-states realized. The cycle dict must carry `cv_realized`,
    event-sourced by monitor_cycle at the moment it observes a count drop (it
    already classifies leg_tp vs leg_closed_other with mid in hand). Without it,
    any closed leg forces ok=False; the TP-priced figure is kept in the book as
    a best-effort estimate only.
    """
    notes: list[str] = []
    ok = True
    step = float(cyc.get("step") or 0.0)
    fulcrum = float(cyc.get("fulcrum") or 0.0)
    legs: list[BookLeg] = []
    cv_realized = cyc.get("cv_realized")     # event-sourced by monitor_cycle; None = absent
    realized = 0.0
    any_closed = False

    for side in ("buy", "sell"):
        ladder = list(cyc.get(f"book_{side}_legs") or [])
        if not ladder:
            if int(row.get(f"{side}s") or 0) or int(row.get(f"{side}_pendings") or 0):
                ok = False
                notes.append(f"{side}: no arm-time ladder but broker reports legs")
            continue
        # innermost-first: nearest trigger to the fulcrum fills first
        ladder.sort(key=lambda pl: abs(float(pl[0]) - fulcrum))
        tp = float(cyc.get("tp_up" if side == "buy" else "tp_dn") or 0.0)

        open_n = int(row.get(f"{side}s") or 0)
        max_seen = max(int(cyc.get(f"max_{side}s_seen") or 0), open_n)
        closed_n = max_seen - open_n
        pend_n = int(row.get(f"{side}_pendings") or 0)

        if max_seen > len(ladder):
            ok = False
            notes.append(f"{side}: max_seen {max_seen} exceeds ladder {len(ladder)}")
            max_seen = len(ladder)
            closed_n = max(0, max_seen - open_n)
        if closed_n and open_n:
            # a whole-side TP would have closed everything; a partial close means
            # leg identity is gone (CLOSE_SIDE frac / leg_closed_other / manual)
            ok = False
            notes.append(f"{side}: partial close ({closed_n} closed, {open_n} open)")
        if pend_n > len(ladder) - max_seen:
            ok = False
            notes.append(f"{side}: {pend_n} pendings > {len(ladder) - max_seen} unfilled legs")
            pend_n = len(ladder) - max_seen

        # closed innermost legs — TP-priced ESTIMATE only (see docstring); trusted
        # realized must come from cv_realized
        if closed_n:
            any_closed = True
        for price, lot in ladder[:closed_n]:
            price, lot = float(price), float(lot)
            if tp > 0:
                diff = (tp - price) if side == "buy" else (price - tp)
                realized += diff * lot * contract
            else:
                ok = False
                notes.append(f"{side}: closed legs but no side TP to price them")
                break
        # open legs
        for price, lot in ladder[closed_n:max_seen]:
            legs.append(BookLeg(side=side, price=float(price), qty=float(lot),
                                filled=True, tp=tp))
        # surviving pendings — innermost of the unfilled remainder
        for price, lot in ladder[max_seen:max_seen + pend_n]:
            legs.append(BookLeg(side=side, price=float(price), qty=float(lot),
                                filled=False, tp=tp))

        want_lots = float(row.get(f"{side}_lots") or 0.0)
        got_lots = sum(l.qty for l in legs if l.filled and l.side == side)
        if abs(got_lots - want_lots) > lots_tol:
            ok = False
            notes.append(f"{side}: reconstructed lots {got_lots:.3f} != reported {want_lots:.3f}")

        # Floating-pnl cross-check: the EA reports per-side floating PnL; marking the
        # reconstructed legs to mid must agree. Catches leg-identity drift (fills out
        # of innermost order after pend-shift refresh / re-placement) that the lot-sum
        # check misses — and only when it is economically material, which is the right
        # sensitivity: a swap with identical value is harmless to the value function.
        if f"{side}_pnl" in row:
            sign = 1.0 if side == "buy" else -1.0
            got_pnl = sum(sign * (mid - l.price) * l.qty * contract
                          for l in legs if l.filled and l.side == side)
            want_pnl = float(row.get(f"{side}_pnl") or 0.0)
            tol = pnl_tol if pnl_tol > 0 else max(0.05, 0.01 * abs(want_pnl))
            if abs(got_pnl - want_pnl) > tol:
                ok = False
                notes.append(f"{side}: reconstructed floating {got_pnl:.4f} != "
                             f"reported {want_pnl:.4f}")

    if cv_realized is not None:
        realized = float(cv_realized)
    elif any_closed:
        ok = False
        notes.append("closures unverifiable without cv_realized (TP-priced estimate kept)")

    book = CycleBook(legs=tuple(legs), realized=realized, contract=contract,
                     spread=spread, mid=mid, step=step, fulcrum=fulcrum)
    return book, ok, notes
