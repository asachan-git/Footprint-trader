"""monitor_cycle integration with cycle_value (execution/exec_bridge.py).

Drives ExecBridge.monitor_cycle directly against a synthetic arm — no Flask, no
harness. Covers: (1) cv_realized event-sourcing on a side TP close, (2) the cv
trail arm/retrace/fire sequence end-to-end via poll-shaped calls, (3) cv OFF by
default leaves bias_trail/net_target behavior untouched (regression guard).
"""
import pytest

from execution.exec_bridge import ExecBridge, CLOSE_ALL, CANCEL_PENDINGS, MODIFY_POSITION


ACCOUNT = "test-acct"
SYMBOL = "XAUUSD+"
MAGIC = 990001
SETTINGS = {
    "execution": {"contract_size": {SYMBOL: 100.0}},
    "grid_levels": {
        "bias_trail_enabled": True,
        "cycle_net_target_usd": 0.0,     # keep legacy exits inert unless a test wants them
        "cycle_close_on_full_hedge": False,
        "cycle_value": {
            "enabled": True,
            "trail_step_mult": 1.0,
            "floor_usd_by_tf": {"1m": 0.0},
            "max_loss_usd_by_tf": {"1m": 1000.0},
            "broker_sl_backstop": True,
            "recon_lots_tol": 0.005,
        },
    },
}


@pytest.fixture(autouse=True)
def _clean_state():
    ExecBridge._last_arm.clear()
    ExecBridge._open.clear()
    ExecBridge._quotes.clear()
    with ExecBridge._lock:
        ExecBridge._cmds.clear()
        ExecBridge._seq.clear()
    yield


def _arm(buy_legs, sell_legs, fulcrum, step, tp_up, tp_down):
    ExecBridge.set_last_arm(
        ACCOUNT, SYMBOL, tf="1m", magic=MAGIC, fulcrum=fulcrum, step=step,
        buy_n=len(buy_legs), sell_n=len(sell_legs),
        book_buy_legs=buy_legs, book_sell_legs=sell_legs,
        tp_up=tp_up, tp_down=tp_down, active=True, armed_tf="1m",
        max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
        bias_peak=0.0, bias_booked=False, bias_trail_done=False,
        be_done_buy=False, be_done_sell=False,
    )


def _poll(mid, buys, sells, buy_pnl, sell_pnl, buy_lots, sell_lots,
         buy_pendings, sell_pendings, pnl=None):
    ExecBridge.set_quote(ACCOUNT, SYMBOL, bid=mid, ask=mid)
    ExecBridge.set_open(ACCOUNT, SYMBOL, positions=buys + sells,
                        pendings=buy_pendings + sell_pendings, tf="1m", magic=MAGIC,
                        buys=buys, sells=sells,
                        buy_pendings=buy_pendings, sell_pendings=sell_pendings)
    return ExecBridge.monitor_cycle(
        ACCOUNT, SYMBOL, SETTINGS, pnl=pnl if pnl is not None else buy_pnl + sell_pnl,
        buys=buys, sells=sells, tf="1m", magic=MAGIC,
        buy_pnl=buy_pnl, sell_pnl=sell_pnl, buy_lots=buy_lots, sell_lots=sell_lots)


# ── cv_realized event-sourcing ────────────────────────────────────────────────

def test_cv_realized_accrues_on_side_tp_close():
    # single buy leg, fulcrum 100, tp_up 103; goes from filled(1) -> closed(0)
    _arm([[100.0, 0.01]], [], fulcrum=100.0, step=1.0, tp_up=103.0, tp_down=0.0)
    # poll 1: leg fills (buys 0->1 is a FILL not tracked by _cv_note_closes directly,
    # but max_buys_seen high-water updates via monitor_cycle itself)
    _poll(mid=100.5, buys=1, sells=0, buy_pnl=0.5, sell_pnl=0.0,
         buy_lots=0.01, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    cyc = ExecBridge.get_last_arm(ACCOUNT, SYMBOL, magic=MAGIC)
    assert cyc["max_buys_seen"] == 1
    # poll 2: TP hit — buys drops 1->0 at mid=103
    _poll(mid=103.0, buys=0, sells=0, buy_pnl=0.0, sell_pnl=0.0,
         buy_lots=0.0, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    cyc = ExecBridge.get_last_arm(ACCOUNT, SYMBOL, magic=MAGIC)
    # (103 - 100) * 0.01 * contract(100) = 3.0
    assert cyc["cv_realized"] == pytest.approx(3.0)
    assert cyc["cv_closed_buy_n"] == 1


def test_cv_realized_prices_non_tp_close_at_mid():
    # SL/other close (mid far from tp_up) — priced at mid, not tp
    _arm([[100.0, 0.01]], [], fulcrum=100.0, step=1.0, tp_up=110.0, tp_down=0.0)
    _poll(mid=100.5, buys=1, sells=0, buy_pnl=0.5, sell_pnl=0.0,
         buy_lots=0.01, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    _poll(mid=99.0, buys=0, sells=0, buy_pnl=0.0, sell_pnl=0.0,
         buy_lots=0.0, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    cyc = ExecBridge.get_last_arm(ACCOUNT, SYMBOL, magic=MAGIC)
    # (99 - 100) * 0.01 * 100 = -1.0 (a loss, priced at mid since TP wasn't hit)
    assert cyc["cv_realized"] == pytest.approx(-1.0)


# ── cv trail arm / retrace / fire ─────────────────────────────────────────────

def test_cv_trail_arms_backstops_then_fires_on_retrace():
    # long-only ladder: 2 filled buys, no sells. Net long -> UP direction.
    _arm([[100.0, 0.01], [101.0, 0.01]], [], fulcrum=100.0, step=1.0,
        tp_up=200.0, tp_down=0.0)   # tp far away so no TP interference
    _poll(mid=101.5, buys=2, sells=0, buy_pnl=1.5, sell_pnl=0.0,
         buy_lots=0.02, sell_lots=0.0, buy_pendings=0, sell_pendings=0)

    # F=0, d=step=1.0: p_act is reachable once locked(p-d) >= 0. Push mid up until armed.
    r = _poll(mid=103.0, buys=2, sells=0, buy_pnl=3.5, sell_pnl=0.0,
             buy_lots=0.02, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    cyc = ExecBridge.get_last_arm(ACCOUNT, SYMBOL, magic=MAGIC)
    assert cyc.get("cv_armed") is True
    assert cyc.get("cv_peak") == pytest.approx(103.0)
    assert r is None   # arming doesn't flatten — cycle continues

    # backstop SL + adverse-side CANCEL_PENDINGS were enqueued at arm
    cmds = list(ExecBridge._cmds.values())
    assert any(c.type == MODIFY_POSITION and c.comment == "FB|cv_backstop" for c in cmds)
    assert any(c.type == CANCEL_PENDINGS and c.side == "sell" for c in cmds)

    # price rises further -> peak ratchets, no fire
    _poll(mid=104.0, buys=2, sells=0, buy_pnl=4.5, sell_pnl=0.0,
         buy_lots=0.02, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    cyc = ExecBridge.get_last_arm(ACCOUNT, SYMBOL, magic=MAGIC)
    assert cyc["cv_peak"] == pytest.approx(104.0)

    # retrace by d=1.0 from peak 104 -> 103 fires cv_trail
    r = _poll(mid=103.0, buys=2, sells=0, buy_pnl=3.5, sell_pnl=0.0,
             buy_lots=0.02, sell_lots=0.0, buy_pendings=0, sell_pendings=0)
    assert r == "cv_trail"
    cmds = list(ExecBridge._cmds.values())
    assert any(c.type == CLOSE_ALL and c.comment == "FB|flatten|cv_trail" for c in cmds)


def test_cv_kill_fires_on_projected_loss_beyond_max():
    _arm([[100.0, 0.01]], [], fulcrum=100.0, step=1.0, tp_up=200.0, tp_down=0.0)
    settings = {**SETTINGS, "grid_levels": {**SETTINGS["grid_levels"],
                "cycle_value": {**SETTINGS["grid_levels"]["cycle_value"],
                                "max_loss_usd_by_tf": {"1m": 0.5}}}}
    ExecBridge.set_quote(ACCOUNT, SYMBOL, bid=95.0, ask=95.0)
    ExecBridge.set_open(ACCOUNT, SYMBOL, positions=1, pendings=0, tf="1m", magic=MAGIC,
                        buys=1, sells=0, buy_pendings=0, sell_pendings=0)
    r = ExecBridge.monitor_cycle(ACCOUNT, SYMBOL, settings, pnl=-5.0, buys=1, sells=0,
                                 tf="1m", magic=MAGIC, buy_pnl=-5.0, sell_pnl=0.0,
                                 buy_lots=0.01, sell_lots=0.0)
    assert r == "cv_kill"


# ── regression: flag off leaves legacy exits untouched ────────────────────────

def test_cv_disabled_by_default_legacy_net_target_still_fires():
    settings = {**SETTINGS, "grid_levels": {**SETTINGS["grid_levels"],
                "cycle_value": {"enabled": False},
                "cycle_net_target_usd": 1.0}}
    _arm([[100.0, 0.01]], [], fulcrum=100.0, step=1.0, tp_up=200.0, tp_down=0.0)
    ExecBridge.set_quote(ACCOUNT, SYMBOL, bid=102.0, ask=102.0)
    ExecBridge.set_open(ACCOUNT, SYMBOL, positions=1, pendings=0, tf="1m", magic=MAGIC,
                        buys=1, sells=0, buy_pendings=0, sell_pendings=0)
    r = ExecBridge.monitor_cycle(ACCOUNT, SYMBOL, settings, pnl=2.0, buys=1, sells=0,
                                 tf="1m", magic=MAGIC, buy_pnl=2.0, sell_pnl=0.0,
                                 buy_lots=0.01, sell_lots=0.0)
    assert r == "net_target"
