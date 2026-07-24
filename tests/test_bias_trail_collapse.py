"""Trail-SL must COLLAPSE the whole cycle, not partial-book.

When bias_book_trail fires (a filled side's floating P&L gives back >= giveback%
from its peak), monitor_cycle must enqueue ONE CLOSE_ALL that flattens both sides +
pendings + hedge — then the cycle ends and waits for a fresh trigger. The old
behaviour (CLOSE_SIDE half + MOVE_BE runner, cycle continues) is retired.
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def bridge(monkeypatch):
    import execution.arm_state_store as ass
    monkeypatch.setattr(ass, "_ROOT", Path(tempfile.mkdtemp()))
    from execution.exec_bridge import ExecBridge as E
    E.reset()
    E._last_arm.clear()
    E._last_emit.clear()
    E._open.clear() if hasattr(E, "_open") else None
    return E


def _settings(**over):
    g = {
        "cycle_net_target_usd": 100000.0,   # far away — not the exit under test
        "bias_trail_enabled": True,
        "bias_trail_activate_usd": 5.0,
        "bias_trail_giveback_pct": 40.0,
        "cycle_close_on_full_hedge": False,
    }
    g.update(over)
    return {"grid_levels": g}


def test_trail_hit_enqueues_close_all_not_close_side(bridge):
    E = bridge
    acct, sym, magic = "ACC", "XAUUSD+", 770013
    E.set_quote(acct, sym, 4000.0, 4000.2)
    # buy side fully filled (buys >= buy_n) → the committed bias side
    E.set_open(acct, sym, 3, 0, tf="15m", magic=magic)
    E.set_last_arm(acct, sym, tf="15m", magic=magic, active=True,
                   armed_tf="15m", fulcrum=4000.0, buy_n=3, sell_n=3,
                   tp_up=4010.0, tp_down=3990.0, bias_peak=0.0,
                   bias_booked=False, max_pos_seen=3, ts=1.0)

    # 1) drive the buy-side peak up to 100
    E.monitor_cycle(acct, sym, _settings(), tf="15m", magic=magic,
                    pnl=100.0, buys=3, sells=0, buy_pnl=100.0, sell_pnl=0.0)
    c = E.get_last_arm(acct, sym, magic=magic)
    assert c["bias_peak"] == 100.0 and not c.get("bias_booked")

    # 2) side_pnl gives back to 50 (<= 100*(1-0.40)=60) → trail fires
    reason = E.monitor_cycle(acct, sym, _settings(), tf="15m", magic=magic,
                             pnl=50.0, buys=3, sells=0, buy_pnl=50.0, sell_pnl=0.0)
    assert reason == "bias_book_trail"

    cmds = E.snapshot(acct)
    types = [c["type"] for c in cmds]
    assert "CLOSE_ALL" in types, f"expected CLOSE_ALL, got {types}"
    assert "CLOSE_SIDE" not in types, "trail must NOT partial-book anymore"
    assert "MOVE_BE" not in types, "trail must NOT leave a BE runner anymore"

    # cycle marked flattening (flatten_ts set) so it won't re-fire every poll
    c = E.get_last_arm(acct, sym, magic=magic)
    assert c.get("bias_booked") is True and float(c.get("flatten_ts") or 0) > 0


def test_trail_does_not_fire_below_activate(bridge):
    E = bridge
    acct, sym, magic = "ACC", "XAUUSD+", 770013
    E.set_quote(acct, sym, 4000.0, 4000.2)
    E.set_open(acct, sym, 3, 0, tf="15m", magic=magic)
    E.set_last_arm(acct, sym, tf="15m", magic=magic, active=True,
                   armed_tf="15m", fulcrum=4000.0, buy_n=3, sell_n=3,
                   tp_up=4010.0, tp_down=3990.0, bias_peak=0.0,
                   bias_booked=False, max_pos_seen=3, ts=1.0)
    # peak only 4 (< activate 5) then giveback → must NOT fire
    E.monitor_cycle(acct, sym, _settings(), tf="15m", magic=magic,
                    pnl=4.0, buys=3, sells=0, buy_pnl=4.0, sell_pnl=0.0)
    reason = E.monitor_cycle(acct, sym, _settings(), tf="15m", magic=magic,
                             pnl=1.0, buys=3, sells=0, buy_pnl=1.0, sell_pnl=0.0)
    assert reason is None
    assert "CLOSE_ALL" not in [c["type"] for c in E.snapshot(acct)]
