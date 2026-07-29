"""Analytic cases for execution/cycle_value.py + adapter agreement checks.

Every expected value here is hand-derived from the piecewise-linear model —
the tests are the module's proof, since it is pure math with no I/O.
"""
import math

import pytest

from execution.cycle_value import (
    UP, DOWN, BookLeg, CycleBook,
    pnl_at, breakeven, trail_activation, kill_price, worst_case_loss,
)
from execution.cycle_book import book_from_arm, book_from_broker


def mk(legs, mid, realized=0.0, contract=1.0, spread=0.0, step=1.0, fulcrum=100.0):
    return CycleBook(legs=tuple(legs), realized=realized, contract=contract,
                     spread=spread, mid=mid, step=step, fulcrum=fulcrum)


def buy(price, qty=1.0, filled=True, tp=0.0, sl=0.0):
    return BookLeg("buy", price, qty, filled, tp, sl)


def sell(price, qty=1.0, filled=True, tp=0.0, sl=0.0):
    return BookLeg("sell", price, qty, filled, tp, sl)


# ── single leg ────────────────────────────────────────────────────────────────

def test_single_long_leg_linear():
    b = mk([buy(102.0)], mid=100.0)
    assert pnl_at(b, 105.0) == pytest.approx(3.0)
    assert pnl_at(b, 100.0) == pytest.approx(-2.0)
    assert breakeven(b, UP) == pytest.approx(102.0)
    assert breakeven(b, DOWN) is None          # net long never breaks even downward
    # locked(p) = (p - 102) - d ; >= 0 at p = 103
    assert trail_activation(b, d=1.0, F=0.0, direction=UP) == pytest.approx(103.0)
    px, worst = kill_price(b, 5.0, DOWN)
    assert px == pytest.approx(97.0)
    assert worst == pytest.approx(-5.0)


def test_single_short_leg_mirrored():
    b = mk([sell(100.0)], mid=102.0)
    assert pnl_at(b, 99.0) == pytest.approx(1.0)
    assert breakeven(b, DOWN) == pytest.approx(100.0)
    assert trail_activation(b, d=1.0, F=0.0, direction=DOWN) == pytest.approx(99.0)
    px, _ = kill_price(b, 3.0, UP)
    assert px == pytest.approx(103.0)


# ── kinks ─────────────────────────────────────────────────────────────────────

def test_side_tp_kink_realizes_and_flattens_slope():
    b = mk([buy(100.0, tp=105.0)], mid=100.0)
    assert pnl_at(b, 104.0) == pytest.approx(4.0)
    assert pnl_at(b, 110.0) == pytest.approx(5.0)   # closed at TP, flat beyond


def test_whole_side_tp_full_ladder():
    legs = [buy(101.0, 0.01, tp=106.0), buy(102.0, 0.01, tp=106.0),
            buy(103.0, 0.01, tp=106.0)]
    b = mk(legs, mid=104.0)
    assert breakeven(b, UP) == pytest.approx(104.0)          # already positive
    assert pnl_at(b, 107.0) == pytest.approx((5 + 4 + 3) * 0.01)


def test_pending_fill_adds_slope_and_spread_cost():
    b = mk([buy(102.0, filled=False)], mid=100.0, spread=0.5)
    assert pnl_at(b, 101.0) == pytest.approx(0.0)            # not yet filled
    assert pnl_at(b, 105.0) == pytest.approx(3.0 - 0.5)      # filled at 102, cost once


def test_pending_across_fulcrum_path_dependence():
    # walking down fills the sell stop at 98 — the projection must include it
    b = mk([buy(101.0), sell(98.0, filled=False)], mid=100.0)
    assert pnl_at(b, 97.0) == pytest.approx((97 - 101) + (98 - 97))
    # walking up never touches it
    assert pnl_at(b, 103.0) == pytest.approx(2.0)


def test_pending_fill_then_own_tp_on_same_path():
    b = mk([buy(102.0, filled=False, tp=104.0)], mid=100.0)
    assert pnl_at(b, 110.0) == pytest.approx(2.0)            # fill 102 → close 104


def test_tp_refresh_changes_activation():
    near = mk([buy(100.0, tp=103.0)], mid=100.0)
    far = mk([buy(100.0, tp=110.0)], mid=100.0)
    # d=2, F=0 → activation 102 in both while TP beyond it; nearer TP caps locked
    # value beyond the kink but not the activation price itself here
    assert trail_activation(near, 2.0, 0.0, UP) == pytest.approx(102.0)
    assert trail_activation(far, 2.0, 0.0, UP) == pytest.approx(102.0)
    # floor above what the near TP can lock → near book must go beyond its TP
    # (slope 0 after close, locked frozen at 3.0) → F=4 unreachable
    assert trail_activation(near, 0.0, 4.0, UP) is None
    assert trail_activation(far, 0.0, 4.0, UP) == pytest.approx(104.0)


# ── hedged / degenerate books ────────────────────────────────────────────────

def test_hedged_book_no_breakeven_no_trail():
    b = mk([buy(100.0), sell(100.0)], mid=100.0, realized=-2.0)
    assert breakeven(b, UP) is None
    assert breakeven(b, DOWN) is None
    assert trail_activation(b, 1.0, 0.0, UP) is None
    assert worst_case_loss(b) == pytest.approx(-2.0)


def test_kill_none_when_hedge_locks_first():
    # long from 100, opposite sell stop at 95: max drawdown locks at -5
    b = mk([buy(100.0), sell(95.0, filled=False)], mid=100.0)
    px, worst = kill_price(b, 10.0, DOWN)
    assert px is None
    assert worst == pytest.approx(-5.0)
    px, worst = kill_price(b, 3.0, DOWN)
    assert px == pytest.approx(97.0)


def test_worst_case_unbounded_without_hedge():
    b = mk([buy(100.0)], mid=100.0)
    assert worst_case_loss(b) == -math.inf


# ── monotone ratchet ──────────────────────────────────────────────────────────

def test_ratchet_tp_fill_never_worsens_activation():
    # compare at the SAME reference price: after the side TP banks 0.12, the
    # locked value is 0.12 everywhere, so activation is immediate (current mid)
    before = mk([buy(101.0, 0.01, tp=106.0), buy(102.0, 0.01, tp=106.0),
                 buy(103.0, 0.01, tp=106.0)], mid=104.0)
    after = mk([], mid=104.0, realized=(5 + 4 + 3) * 0.01)
    d, F = 1.0, 0.05
    a0 = trail_activation(before, d, F, UP)
    a1 = trail_activation(after, d, F, UP)
    assert a1 is not None and a0 is not None
    assert a1 <= a0
    assert a1 == pytest.approx(104.0)


# ── adapters agree ────────────────────────────────────────────────────────────

class _FakeBroker:
    def __init__(self, positions=(), pendings=(), events=()):
        self.positions = list(positions)
        self.pendings = list(pendings)
        self.events = list(events)


class _P:
    """Duck-typed fill_engine Position/Pending row."""
    def __init__(self, magic, is_buy, price, lot, tp=0.0, sl=0.0):
        self.magic = magic
        self.is_buy = is_buy
        self.entry = price
        self.price = price
        self.lot = lot
        self.tp = tp
        self.sl = sl


def _cyc(buy_legs, sell_legs, tp_up=0.0, tp_dn=0.0, max_b=0, max_s=0):
    return {"book_buy_legs": buy_legs, "book_sell_legs": sell_legs,
            "tp_up": tp_up, "tp_dn": tp_dn, "step": 1.0, "fulcrum": 100.0,
            "max_buys_seen": max_b, "max_sells_seen": max_s}


def test_adapters_agree_on_clean_cycle():
    # ladder: buys 101/102/103, sells 99/98; two buys filled, rest pending
    magic = 770011
    broker = _FakeBroker(
        positions=[_P(magic, True, 101.0, 0.01, tp=106.0),
                   _P(magic, True, 102.0, 0.02, tp=106.0)],
        pendings=[_P(magic, True, 103.0, 0.03, tp=106.0),
                  _P(magic, False, 99.0, 0.01, tp=96.0),
                  _P(magic, False, 98.0, 0.02, tp=96.0)],
    )
    exact = book_from_broker(broker, magic, mid=102.5, step=1.0, fulcrum=100.0)

    cyc = _cyc([[101.0, 0.01], [102.0, 0.02], [103.0, 0.03]],
               [[99.0, 0.01], [98.0, 0.02]], tp_up=106.0, tp_dn=96.0,
               max_b=2, max_s=0)
    row = {"buys": 2, "sells": 0, "buy_lots": 0.03, "sell_lots": 0.0,
           "buy_pendings": 1, "sell_pendings": 2}
    recon, ok, notes = book_from_arm(cyc, row, mid=102.5, contract=1.0)
    assert ok, notes
    assert recon.realized == pytest.approx(exact.realized)
    key = lambda l: (l.side, l.filled, round(l.price, 6), round(l.qty, 6), round(l.tp, 6))
    assert sorted(map(key, recon.legs)) == sorted(map(key, exact.legs))
    # and the value functions agree on the reconstruction
    assert pnl_at(recon, 105.0) == pytest.approx(pnl_at(exact, 105.0))
    assert trail_activation(recon, 1.0, 0.0, UP) == pytest.approx(
        trail_activation(exact, 1.0, 0.0, UP))


def test_adapter_flags_partial_close_divergence():
    cyc = _cyc([[101.0, 0.01], [102.0, 0.02], [103.0, 0.03]], [],
               tp_up=106.0, max_b=3)
    row = {"buys": 1, "sells": 0, "buy_lots": 0.03, "sell_lots": 0.0,
           "buy_pendings": 0, "sell_pendings": 0}
    _, ok, notes = book_from_arm(cyc, row, mid=102.0, contract=1.0)
    assert not ok
    assert any("partial close" in n for n in notes)


def test_adapter_flags_lot_mismatch():
    cyc = _cyc([[101.0, 0.01], [102.0, 0.02]], [], tp_up=106.0, max_b=2)
    row = {"buys": 2, "sells": 0, "buy_lots": 0.05, "sell_lots": 0.0,
           "buy_pendings": 0, "sell_pendings": 0}
    _, ok, notes = book_from_arm(cyc, row, mid=102.0, contract=1.0)
    assert not ok
    assert any("lots" in n for n in notes)


def test_adapter_whole_side_tp_estimate_needs_cv_realized():
    # all 2 buys closed: WITHOUT cv_realized the closure is unverifiable (could be
    # SL/CLOSE_ALL, not TP) → ok=False but the TP-priced estimate is kept
    cyc = _cyc([[101.0, 0.01], [102.0, 0.02]], [], tp_up=106.0, max_b=2)
    row = {"buys": 0, "sells": 0, "buy_lots": 0.0, "sell_lots": 0.0,
           "buy_pendings": 0, "sell_pendings": 0}
    book, ok, notes = book_from_arm(cyc, row, mid=106.0, contract=100.0)
    assert not ok
    assert any("cv_realized" in n for n in notes)
    assert book.realized == pytest.approx((5 * 0.01 + 4 * 0.02) * 100.0)
    # WITH event-sourced cv_realized the book is trusted and uses it verbatim
    cyc["cv_realized"] = 7.5
    book, ok, notes = book_from_arm(cyc, row, mid=106.0, contract=100.0)
    assert ok, notes
    assert book.realized == pytest.approx(7.5)
