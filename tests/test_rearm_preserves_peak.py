"""A fulcrum shift must not erase the trail's accumulated view.

Observed live 2026-08-20 on magic 770052 (hvn_edge 5m): the cycle re-anchored
at 17:10 and again at 17:30, each writing a fresh arm record with bias_peak
reset to 0.0 while the FILLED legs from the previous fulcrum were still open.
The position reached 800+ USC against a 125 activate threshold and nothing ever
booked, because the peak was zeroed on a 5-minute timer before it could be
given back. No cycle_outcomes row was written either.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge

ACCT, SYM = "REARM", "XAUUSD.pc"
# Distinct magic per test — set_last_arm's carry-over now keys off prev.active +
# prev.max_pos_seen>0 (see fix below), so tests sharing one magic would leak state
# into each other's "first" _arm call (a later test's baseline getting maxed against
# an earlier test's leftover peak). Isolate by magic instead of relying on call order.


def _arm(magic, **kw):
    ExecBridge.set_last_arm(ACCT, SYM, magic=magic, **kw)


def _get(magic):
    return ExecBridge.get_last_arm(ACCT, SYM, magic=magic) or {}


def test_peak_survives_a_reanchor_while_positions_are_open():
    magic = 770201
    _arm(magic, active=True, fulcrum=4481.21, buy_n=2, sell_n=3,
         bias_peak=800.0, bias_booked=False, max_pos_seen=3)
    # fulcrum shift: a fresh arm record on a new fulcrum, no trail fields passed
    _arm(magic, active=True, fulcrum=4461.51, buy_n=2, sell_n=3)
    s = _get(magic)
    assert s["fulcrum"] == 4461.51, "the new fulcrum must apply"
    assert s["bias_peak"] == 800.0, "the peak belongs to the inventory, not the fulcrum"
    assert s["max_pos_seen"] == 3


def test_peak_survives_even_when_the_rearm_passes_an_explicit_zero():
    """The real emit_grid re-arm path always passes bias_peak=0.0/max_pos_seen=0/
    bias_booked=False explicitly (its "fresh arm" defaults) — verified live
    2026-08-20 on magic 770052: prev bias_peak=140.25/max_pos_seen=2 right before a
    re-anchor, then the new record showed bias_peak=0.0/max_pos_seen=0 anyway,
    because the old `k not in meta` guard never fires when meta explicitly has the
    key. This is the actual production path — the previous test's "meta omits the
    keys" case was never what really happens."""
    magic = 770202
    _arm(magic, active=True, fulcrum=4481.21, buy_n=2, sell_n=3,
         bias_peak=140.25, bias_booked=False, max_pos_seen=2)
    _arm(magic, active=True, fulcrum=4461.51, buy_n=2, sell_n=3,
         bias_peak=0.0, bias_booked=False, max_pos_seen=0)
    s = _get(magic)
    assert s["bias_peak"] == 140.25
    assert s["max_pos_seen"] == 2


def test_book_guard_survives_a_reanchor():
    """Otherwise a re-anchor silently re-arms a one-shot book."""
    magic = 770203
    _arm(magic, active=True, fulcrum=4481.0, bias_peak=500.0, bias_booked=True, max_pos_seen=2)
    _arm(magic, active=True, fulcrum=4470.0, bias_booked=False)
    assert _get(magic)["bias_booked"] is True


def test_a_flat_cycle_resets_normally():
    """Reset IS correct when the cycle holds nothing — a genuinely new cycle
    must not inherit the previous one's peak."""
    magic = 770204
    _arm(magic, active=True, fulcrum=4481.0, bias_peak=800.0, bias_booked=True, max_pos_seen=0)
    _arm(magic, active=True, fulcrum=4461.0, bias_peak=0.0, bias_booked=False, max_pos_seen=0)
    s = _get(magic)
    assert s["bias_peak"] == 0.0
    assert s["bias_booked"] is False


def test_a_closed_cycle_does_not_leak_into_a_fresh_one_on_the_same_magic():
    """A magic reused after its previous cycle fully closed (active=False at close,
    per every flatten path) must NOT inherit the old cycle's stale peak."""
    magic = 770205
    _arm(magic, active=True, fulcrum=4481.0, bias_peak=800.0, bias_booked=True, max_pos_seen=3)
    _arm(magic, active=False, fulcrum=4481.0, bias_peak=800.0, bias_booked=True, max_pos_seen=3)
    _arm(magic, active=True, fulcrum=4500.0, bias_peak=0.0, bias_booked=False, max_pos_seen=0)
    s = _get(magic)
    assert s["bias_peak"] == 0.0
    assert s["bias_booked"] is False


def test_an_explicit_update_still_wins():
    """monitor_cycle raising the peak must not be blocked by the carry-over."""
    magic = 770206
    _arm(magic, active=True, fulcrum=4481.0, bias_peak=500.0, max_pos_seen=2)
    _arm(magic, active=True, fulcrum=4481.0, bias_peak=900.0, max_pos_seen=3)
    s = _get(magic)
    assert s["bias_peak"] == 900.0
    assert s["max_pos_seen"] == 3
