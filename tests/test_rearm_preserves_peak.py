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

ACCT, SYM, MAGIC = "REARM", "XAUUSD.pc", 770052


def _arm(**kw):
    ExecBridge.set_last_arm(ACCT, SYM, magic=MAGIC, **kw)


def _get():
    return ExecBridge.get_last_arm(ACCT, SYM, magic=MAGIC) or {}


def test_peak_survives_a_reanchor_while_positions_are_open():
    _arm(active=True, fulcrum=4481.21, buy_n=2, sell_n=3,
         bias_peak=800.0, bias_booked=False, max_pos_seen=3)
    # fulcrum shift: a fresh arm record on a new fulcrum, no trail fields passed
    _arm(active=True, fulcrum=4461.51, buy_n=2, sell_n=3)
    s = _get()
    assert s["fulcrum"] == 4461.51, "the new fulcrum must apply"
    assert s["bias_peak"] == 800.0, "the peak belongs to the inventory, not the fulcrum"
    assert s["max_pos_seen"] == 3


def test_book_guard_survives_a_reanchor():
    """Otherwise a re-anchor silently re-arms a one-shot book."""
    _arm(active=True, fulcrum=4481.0, bias_peak=500.0, bias_booked=True, max_pos_seen=2)
    _arm(active=True, fulcrum=4470.0)
    assert _get()["bias_booked"] is True


def test_a_flat_cycle_resets_normally():
    """Reset IS correct when the cycle holds nothing — a genuinely new cycle
    must not inherit the previous one's peak."""
    _arm(active=True, fulcrum=4481.0, bias_peak=800.0, bias_booked=True, max_pos_seen=0)
    _arm(active=True, fulcrum=4461.0, bias_peak=0.0, bias_booked=False, max_pos_seen=0)
    s = _get()
    assert s["bias_peak"] == 0.0
    assert s["bias_booked"] is False


def test_an_explicit_update_still_wins():
    """monitor_cycle raising the peak must not be blocked by the carry-over."""
    _arm(active=True, fulcrum=4481.0, bias_peak=500.0, max_pos_seen=2)
    _arm(active=True, fulcrum=4481.0, bias_peak=900.0, max_pos_seen=3)
    s = _get()
    assert s["bias_peak"] == 900.0
    assert s["max_pos_seen"] == 3
