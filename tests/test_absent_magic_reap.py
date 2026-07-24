"""Absent-magic reap: a magic the EA stops reporting is flat in MT5 → retire it.

The EA lists a magic only while it holds a live order/position, so a magic that
drops out of the poll's `magics` array was manually flattened. The server must
retire the phantom-active arm record (else it lingers active and its fulcrum dedup
stays stuck). A freshly-armed cycle whose legs haven't been reported yet is spared
by a placement grace — unless the EA reports the whole account flat.
"""

import tempfile
import time
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
    return E


def _reap(E, account, sym, body):
    """Mirror the poll-loop reap block against the given EA poll body."""
    magics = body.get("magics")
    if not (sym and isinstance(magics, list)):
        return []
    now = time.time()
    grace = 30.0
    flat_all = (int(body.get("buys", 0) or 0) == 0
                and int(body.get("sells", 0) or 0) == 0
                and int(body.get("pendings", 0) or 0) == 0
                and not magics)
    reported = {int(m.get("magic", 0)) for m in magics}
    reaped = []
    for mg in E.active_magics(account, sym):
        if mg in reported:
            continue
        cyc = E.get_last_arm(account, sym, magic=mg) or {}
        if not flat_all and (now - float(cyc.get("ts", 0.0) or 0.0)) < grace:
            continue
        if cyc:
            E._save_cyc(account, sym, mg, cyc, active=False)
        E.clear_emit(account, sym, magic=mg)
        reaped.append(mg)
    return reaped


def test_active_magics_lists_only_active(bridge):
    E = bridge
    E.set_last_arm("A", "S", magic=7711, active=True, ts=1.0)
    E.set_last_arm("A", "S", magic=7712, active=False, ts=1.0)
    E.set_last_arm("A", "S", magic=7713, active=True, ts=1.0)
    assert sorted(E.active_magics("A", "S")) == [7711, 7713]


def test_absent_magic_reaped(bridge):
    E = bridge
    # armed long ago (past grace) — EA no longer reports it → reap
    E.set_last_arm("A", "S", magic=7711, active=True, ts=time.time() - 100)
    reaped = _reap(E, "A", "S", {"magics": [{"magic": 7712, "buys": 1, "sells": 0}],
                                 "buys": 1, "sells": 0, "pendings": 0})
    assert reaped == [7711]
    assert E.get_last_arm("A", "S", magic=7711)["active"] is False


def test_reported_magic_kept(bridge):
    E = bridge
    E.set_last_arm("A", "S", magic=7711, active=True, ts=time.time() - 100)
    reaped = _reap(E, "A", "S", {"magics": [{"magic": 7711, "buys": 2, "sells": 0}],
                                 "buys": 2, "sells": 0, "pendings": 0})
    assert reaped == []
    assert E.get_last_arm("A", "S", magic=7711)["active"] is True


def test_fresh_arm_spared_by_grace(bridge):
    E = bridge
    # just armed — legs not yet reported. NOT flat-all (a sibling holds positions).
    E.set_last_arm("A", "S", magic=7711, active=True, ts=time.time())
    reaped = _reap(E, "A", "S", {"magics": [{"magic": 7712, "buys": 1, "sells": 0}],
                                 "buys": 1, "sells": 0, "pendings": 0})
    assert reaped == []
    assert E.get_last_arm("A", "S", magic=7711)["active"] is True


def test_flat_all_reaps_even_fresh(bridge):
    E = bridge
    # fresh arm, but EA reports the WHOLE account flat + empty magics → reap regardless
    E.set_last_arm("A", "S", magic=7711, active=True, ts=time.time())
    reaped = _reap(E, "A", "S", {"magics": [], "buys": 0, "sells": 0, "pendings": 0})
    assert reaped == [7711]
    assert E.get_last_arm("A", "S", magic=7711)["active"] is False
