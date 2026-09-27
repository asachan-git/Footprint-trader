"""ExecBridge.monitor_cycle — full-fill orphan guard, disaster cap, and the flatten-rest
races around them. Pure in-memory: disk persistence and audit logs are stubbed.

    pytest tests/execution/test_monitor_cycle_exits.py -q
"""
from __future__ import annotations

import pytest

import execution.exec_bridge as eb
from execution.exec_bridge import (CANCEL_PENDINGS, CLOSE_ALL, CLOSE_SIDE, MOVE_BE,
                                   ExecBridge)

ACC, SYM, MAGIC, TF = "demo1", "XAUUSD+", 774115, "15m"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in ("persist_arm", "persist_emit", "_emit_exit_audit"):
        monkeypatch.setattr(eb, name, lambda *a, **k: None)
    monkeypatch.setattr(eb, "_emit_cycle_outcome", lambda *a, **k: None)
    monkeypatch.setattr(ExecBridge, "_audit", classmethod(lambda cls, *a, **k: None))
    ExecBridge.reset()
    ExecBridge.set_quote(ACC, SYM, 2650.0, 2650.2, now=0.0)
    yield
    ExecBridge.reset()


def settings(**over):
    g = dict(base_lot=0.01, fullfill_be_enabled=True, fullfill_cancel_opposite=True,
             fullfill_close_opposite_filled=True, cycle_max_loss_pts=140,
             cycle_net_target_by_tf={TF: 66.0}, cycle_hedge_decay_pct=33.0,
             cycle_min_target_usd=0.33, bias_trail_enabled=True,
             bias_trail_activate_usd=6.67, bias_trail_giveback_pct=35.0,
             bias_book_frac=0.5, cycle_close_on_full_hedge=True,
             defer_sl_on_half_fill=False)
    g.update(over)
    return {"grid_levels": g}


def arm(buy_n=3, sell_n=4, **extra):
    cyc = dict(active=True, ts=1.0, trigger_kind="hvn_inside_touch", fulcrum=2650.0,
               n_per_side=3, buy_n=buy_n, sell_n=sell_n, tp_up=2660.0, tp_down=2640.0,
               vp_frozen=True)   # skip the VP snapshot (needs the live store)
    cyc.update(extra)
    ExecBridge.set_last_arm(ACC, SYM, magic=MAGIC, **cyc)


def poll(buys, sells, pendings, pnl=0.0, buy_pnl=None, sell_pnl=None, t=10.0, st=None):
    """One EA report: set the open state, then run the monitor like the route does."""
    ExecBridge.set_open(ACC, SYM, buys + sells, pendings, tf=TF, magic=MAGIC,
                        buys=buys, sells=sells, now=t)
    if buy_pnl is None:
        buy_pnl, sell_pnl = pnl, 0.0
    return ExecBridge.monitor_cycle(ACC, SYM, st or settings(), pnl=pnl, buys=buys,
                                    sells=sells, now=t, tf=TF, magic=MAGIC,
                                    buy_pnl=buy_pnl, sell_pnl=sell_pnl)


def cmds():
    return [ExecBridge._cmds[i] for i in ExecBridge._seq]


def kinds():
    return [(c.type, c.side) for c in cmds()]


def drain():
    ExecBridge._cmds.clear()
    ExecBridge._seq.clear()


# ── full-fill + orphan guard ────────────────────────────────────────────────────

def test_fullfill_cancels_closes_orphans_and_moves_be_in_order():
    arm()
    poll(buys=3, sells=1, pendings=3, pnl=5.0, buy_pnl=8.0, sell_pnl=-3.0)
    k = kinds()
    assert k[:3] == [(CANCEL_PENDINGS, "sell"), (CLOSE_SIDE, "sell"), (MOVE_BE, "buy")]
    close = [c for c in cmds() if c.type == CLOSE_SIDE][0]
    assert close.frac == 1.0 and "fullfill_close_opp" in close.comment
    assert (CLOSE_ALL, "") not in k


def test_fullfill_without_filled_opposite_sends_no_close_side():
    arm()
    poll(buys=3, sells=0, pendings=4, pnl=5.0)
    assert kinds()[:2] == [(CANCEL_PENDINGS, "sell"), (MOVE_BE, "buy")]
    assert all(c.type != CLOSE_SIDE for c in cmds())


def test_fullfill_fires_once_per_side():
    arm()
    poll(buys=3, sells=1, pendings=3, pnl=1.0, buy_pnl=2.0, sell_pnl=-1.0)
    drain()
    poll(buys=3, sells=1, pendings=3, pnl=1.0, buy_pnl=2.0, sell_pnl=-1.0, t=11.0)
    assert not any(c.type in (CANCEL_PENDINGS, MOVE_BE) for c in cmds())


def test_orphan_guard_can_be_disabled():
    arm()
    poll(buys=3, sells=1, pendings=3, pnl=1.0, buy_pnl=2.0, sell_pnl=-1.0,
         st=settings(fullfill_close_opposite_filled=False))
    assert all(c.type != CLOSE_SIDE for c in cmds())


def test_sell_side_fullfill_mirrors():
    arm()
    poll(buys=2, sells=4, pendings=1, pnl=2.0, buy_pnl=-4.0, sell_pnl=6.0)
    assert kinds()[:3] == [(CANCEL_PENDINGS, "buy"), (CLOSE_SIDE, "buy"), (MOVE_BE, "sell")]


def test_no_flatten_rest_while_fullfill_cancel_is_in_flight():
    """The poll after fullfill can still show the old pendings (cancel not landed yet)
    while the orphan close HAS landed. That must not look like a mid-cycle leg close."""
    arm()
    poll(buys=3, sells=1, pendings=3, pnl=1.0, buy_pnl=2.0, sell_pnl=-1.0, t=10.0)
    drain()
    r = poll(buys=3, sells=0, pendings=3, pnl=2.0, buy_pnl=2.0, sell_pnl=0.0, t=11.0)
    assert r is None
    assert all(c.type != CLOSE_ALL for c in cmds())


def test_be_stop_on_committed_side_does_not_flatten_runner():
    """After fullfill the committed side is at BE; one leg stopping at BE with no
    pendings left is not a flatten-rest event."""
    arm()
    poll(buys=3, sells=0, pendings=4, pnl=1.0, t=10.0)
    drain()
    r = poll(buys=2, sells=0, pendings=0, pnl=0.5, t=20.0)
    assert r is None and all(c.type != CLOSE_ALL for c in cmds())


# ── disaster cap ────────────────────────────────────────────────────────────────

def test_disaster_cap_flattens_at_threshold():
    arm()
    r = poll(buys=2, sells=2, pendings=3, pnl=-140.0, buy_pnl=-100.0, sell_pnl=-40.0)
    assert r == "max_loss"
    assert kinds() == [(CLOSE_ALL, "")]
    assert "max_loss" in cmds()[0].comment


def test_disaster_cap_not_hit_above_threshold():
    arm()
    r = poll(buys=2, sells=2, pendings=3, pnl=-139.0, buy_pnl=-100.0, sell_pnl=-39.0)
    assert r != "max_loss"


def test_disaster_cap_scales_with_base_lot():
    arm()
    r = poll(buys=2, sells=1, pendings=3, pnl=-300.0, buy_pnl=-300.0, sell_pnl=0.0,
             st=settings(base_lot=0.03))          # cap = 140 × 0.03 × 100 = 420
    assert r != "max_loss"


def test_disaster_cap_uses_account_currency_multiplier():
    """Cent account: P&L arrives in USC, so the cap must be ×100 in native units."""
    arm()
    st = settings(account_ccy_per_usd=100.0)      # cap = 140 × 0.01 × 100 × 100 = 14,000 USC
    assert poll(buys=2, sells=1, pendings=3, pnl=-5000.0, buy_pnl=-5000.0, sell_pnl=0.0, st=st) != "max_loss"
    assert poll(buys=2, sells=1, pendings=3, pnl=-14000.0, buy_pnl=-14000.0, sell_pnl=0.0, st=st, t=12.0) == "max_loss"


def test_disaster_cap_off_when_zero():
    arm()
    r = poll(buys=2, sells=2, pendings=3, pnl=-10_000.0, buy_pnl=-5000.0, sell_pnl=-5000.0,
             st=settings(cycle_max_loss_pts=0))
    assert r != "max_loss"


def test_disaster_cap_not_restacked_while_flatten_pending():
    arm()
    poll(buys=2, sells=2, pendings=3, pnl=-200.0, buy_pnl=-150.0, sell_pnl=-50.0, t=10.0)
    drain()
    r = poll(buys=2, sells=2, pendings=3, pnl=-210.0, buy_pnl=-150.0, sell_pnl=-60.0, t=11.0)
    assert r is None and cmds() == []


# ── bias-trail book → flatten-rest race ─────────────────────────────────────────

def test_bias_book_then_partial_close_does_not_flatten_rest():
    """Trail books half of the winning side while the opposite ladder still rests.
    Positions drop when the book lands; that drop must not fire flatten-rest on any
    later poll (the book is a planned partial close, not a leg TP)."""
    arm(buy_n=4, sell_n=4)
    poll(buys=2, sells=0, pendings=6, pnl=10.0, buy_pnl=10.0, sell_pnl=0.0, t=10.0)  # peak 10
    r = poll(buys=2, sells=0, pendings=6, pnl=6.0, buy_pnl=6.0, sell_pnl=0.0, t=11.0)  # −40% → book
    assert r == "bias_book_trail"
    drain()
    out = [poll(buys=1, sells=0, pendings=6, pnl=3.0, buy_pnl=3.0, sell_pnl=0.0, t=12.0 + i)
           for i in range(3)]
    assert all(o is None for o in out), out
    assert all(c.type != CLOSE_ALL for c in cmds())


def test_real_leg_tp_still_triggers_flatten_rest():
    arm(buy_n=4, sell_n=4)
    poll(buys=2, sells=0, pendings=6, pnl=3.0, buy_pnl=3.0, sell_pnl=0.0, t=10.0)
    ExecBridge.set_quote(ACC, SYM, 2660.0, 2660.2, now=11.0)                 # at tp_up
    r = poll(buys=1, sells=0, pendings=6, pnl=4.0, buy_pnl=4.0, sell_pnl=0.0, t=11.0)
    assert r == "leg_tp"


def test_cycle_that_ends_leg_by_leg_writes_an_outcome_row(monkeypatch):
    rows = []
    monkeypatch.setattr(eb, "_emit_cycle_outcome", lambda cyc, **k: rows.append(k))
    arm()
    poll(buys=3, sells=0, pendings=4, pnl=1.0, t=10.0)       # fullfill: cancel sells, BE buys
    poll(buys=0, sells=0, pendings=0, pnl=0.0, t=30.0)       # BE stops hit, flat
    assert [r["exit_reason"] for r in rows] == ["all_closed"]
    assert ExecBridge.get_last_arm(ACC, SYM, magic=MAGIC)["active"] is False


def test_never_filled_cycle_writes_no_outcome_row(monkeypatch):
    rows = []
    monkeypatch.setattr(eb, "_emit_cycle_outcome", lambda cyc, **k: rows.append(k))
    arm()
    poll(buys=0, sells=0, pendings=7, t=10.0)
    poll(buys=0, sells=0, pendings=0, t=20.0)                # pendings expired/cancelled
    assert rows == []
