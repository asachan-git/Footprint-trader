"""Crash-safe arm-state persistence — a restart must re-adopt live cycles.

The core guarantee: set_last_arm mirrors to disk; load_persisted_state replays it;
and a restored cycle (whose body has NO magic — the store strips it) can be mutated
without collapsing the write to magic=0.
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def store_tmp(monkeypatch):
    """Point the JSONL store at a fresh temp dir and give a clean ExecBridge."""
    import execution.arm_state_store as ass
    monkeypatch.setattr(ass, "_ROOT", Path(tempfile.mkdtemp()))
    from execution.exec_bridge import ExecBridge as E
    E._last_arm.clear()
    E._last_emit.clear()
    return E


def test_arm_survives_a_restart(store_tmp):
    E = store_tmp
    E.set_last_arm("ACC", "XAUUSD+", tf="15m", magic=770013, active=True,
                   fulcrum=4029.0, tp_up=4035.0, net_target_usd=5000.0,
                   buy_n=3, sell_n=3, bias_peak=0.0, ts=1.0)
    E._last_arm.clear()                         # simulate a Flask restart
    assert E.get_last_arm("ACC", "XAUUSD+", magic=770013) is None

    res = E.load_persisted_state()
    assert res["arms"] == 1 and res["active"] == 1
    c = E.get_last_arm("ACC", "XAUUSD+", magic=770013)
    assert c is not None
    assert c["fulcrum"] == 4029.0 and c["net_target_usd"] == 5000.0 and c["active"] is True


def test_restored_cycle_has_no_body_magic(store_tmp):
    # the store keys by (account, symbol, magic) and strips magic from the body.
    E = store_tmp
    E.set_last_arm("A", "S", magic=7713, active=True, fulcrum=100.0, ts=1.0)
    E._last_arm.clear()
    E.load_persisted_state()
    c = E.get_last_arm("A", "S", magic=7713)
    assert "magic" not in c                     # stripped on load


def test_save_cyc_does_not_collapse_to_magic_zero(store_tmp):
    # THE landmine: mutating a magic-less restored cyc via a bare **cyc spread would
    # write to magic=0. _save_cyc must keep it keyed and re-stamp magic.
    E = store_tmp
    E.set_last_arm("A", "S", magic=7713, active=True, fulcrum=100.0, bias_peak=0.0, ts=1.0)
    E._last_arm.clear()
    E.load_persisted_state()
    cyc = E.get_last_arm("A", "S", magic=7713)

    E._save_cyc("A", "S", 7713, cyc, bias_peak=999.0)

    assert E.get_last_arm("A", "S", magic=0) is None            # did NOT leak to 0
    r = E.get_last_arm("A", "S", magic=7713)
    assert r["bias_peak"] == 999.0 and r["magic"] == 7713


def test_reconcile_reactivates_reaped_cycle_with_live_positions(store_tmp):
    E = store_tmp
    E.set_last_arm("A", "S", magic=7712, active=False, fulcrum=100.0,
                   buy_n=2, sell_n=2, max_pos_seen=4, ts=1.0)
    # EA reports the magic still holds positions
    E.reconcile_from_poll("A", "S", [{"magic": 7712, "buys": 2, "sells": 1}])
    c = E.get_last_arm("A", "S", magic=7712)
    assert c["active"] is True                  # reactivated
    assert c["magic"] == 7712                    # not collapsed


def test_reconcile_stubs_a_fully_orphaned_magic(store_tmp):
    E = store_tmp   # no arm record at all, but EA reports positions
    E.reconcile_from_poll("A", "S", [{"magic": 770011, "buys": 1, "sells": 2}])
    c = E.get_last_arm("A", "S", magic=770011)
    assert c is not None and c["active"] is True
    assert c["trigger_kind"] == "recovered" and c["buy_n"] == 1 and c["sell_n"] == 2


def test_reconcile_ignores_flat_magics(store_tmp):
    E = store_tmp
    E.reconcile_from_poll("A", "S", [{"magic": 770011, "buys": 0, "sells": 0}])
    assert E.get_last_arm("A", "S", magic=770011) is None    # nothing to adopt


def test_emit_dedup_survives_restart(store_tmp):
    E = store_tmp
    E.mark_emit("A", "S", 4029.5, magic=7713)
    E._last_emit.clear()
    E.load_persisted_state()
    assert E._last_emit.get(("A", "S", 7713)) == 4029.5

    E.clear_emit("A", "S", magic=7713)          # cleared → None on reload
    E._last_emit.clear()
    E.load_persisted_state()
    assert ("A", "S", 7713) not in E._last_emit
