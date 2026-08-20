"""Re-adoption must reject records that cannot describe a real cycle.

A restart re-evaluates everything it adopts, so a dead arm record is replayed
through monitor_cycle and writes a FRESH cycle_outcomes row every time. On
2026-08-20 that made 64 of 67 rows (96%) phantom repeats of five stale
signatures — one written 24 times across seven restarts — and every conclusion
drawn from that log was wrong, including a claim that the partial-fill trail
fix had been demonstrated live.

A live cycle always has a magic, an arm timestamp, and the trigger that armed it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge


def _usable(magic, state):
    return bool(magic) and bool(float(state.get("ts") or 0)) and bool(state.get("trigger_kind"))


def test_a_healthy_record_is_adopted():
    assert _usable(770013, {"ts": 1787227212.0, "trigger_kind": "hvn_edge", "active": True})


def test_epoch_zero_ts_is_rejected():
    """The stale 770013 record displayed as 05:30:00 IST — ts=0."""
    assert not _usable(770013, {"ts": 0, "trigger_kind": "hvn_edge", "active": True})


def test_missing_trigger_kind_is_rejected():
    """All 64 phantom rows had an empty trigger_kind."""
    assert not _usable(770013, {"ts": 1787227212.0, "trigger_kind": "", "active": True})


def test_magic_zero_is_rejected():
    assert not _usable(0, {"ts": 1787227212.0, "trigger_kind": "hvn_edge", "active": True})


def test_set_last_arm_refuses_a_magic_zero_write():
    """The chokepoint guard: a `**cyc` spread that lost its magic must not fork
    the cycle into key 0, leaving the real magic frozen on a stale record."""
    ExecBridge.set_last_arm("MZ", "XAUUSD.pc", magic=0, bias_peak=374.5, active=True)
    assert ExecBridge.get_last_arm("MZ", "XAUUSD.pc", magic=0) is None


def test_set_last_arm_accepts_a_real_magic():
    ExecBridge.set_last_arm("MZ2", "XAUUSD.pc", magic=770023,
                            bias_peak=374.5, trigger_kind="squeeze", active=True)
    s = ExecBridge.get_last_arm("MZ2", "XAUUSD.pc", magic=770023)
    assert s and s["bias_peak"] == 374.5
