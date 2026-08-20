"""reconcile_from_poll must reactivate a genuine arm record left inactive by the
reap-race, not skip it forever.

Observed live 2026-08-20 on magic 770013 (hvn_inside_touch 15m): the cycle was
armed and reaped in the same poll tick (see the absent-magic reap grace fix —
the whole-account-flat bypass let a brand-new arm get reaped before the EA had
echoed its just-placed legs back). reconcile_from_poll's `if existing: continue`
then treated that inactive-but-real record as "already known" and refused to
touch it, so the cycle sat orphaned with a genuine, growing floating profit
(+$597.75 observed) that nothing ever tracked or trailed.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge

ACCT, SYM, MAGIC = "RECONCILE", "XAUUSD.pc", 770013


def _get():
    return ExecBridge.get_last_arm(ACCT, SYM, magic=MAGIC) or {}


def test_reconcile_reactivates_an_inactive_record_with_live_positions():
    # a genuine arm record, left inactive by the reap race, real geometry intact
    ExecBridge.set_last_arm(ACCT, SYM, magic=MAGIC, active=False, fulcrum=4481.55,
                            trigger_kind="hvn_inside_touch", tp_up=4521.55, tp_down=4457.28,
                            buy_n=3, sell_n=3, bias_peak=0.0, max_pos_seen=0)
    stubbed = ExecBridge.reconcile_from_poll(ACCT, SYM, [
        {"magic": MAGIC, "buys": 1, "sells": 0},
    ])
    assert MAGIC in stubbed
    s = _get()
    assert s["active"] is True
    # geometry must survive — this is a reactivation, not a blank stub overwrite
    assert s["fulcrum"] == 4481.55
    assert s["trigger_kind"] == "hvn_inside_touch"
    assert s["tp_up"] == 4521.55


def test_reconcile_leaves_an_already_active_record_alone():
    ExecBridge.set_last_arm(ACCT, SYM, magic=MAGIC, active=True, fulcrum=9999.0,
                            bias_peak=42.0, max_pos_seen=1)
    stubbed = ExecBridge.reconcile_from_poll(ACCT, SYM, [
        {"magic": MAGIC, "buys": 1, "sells": 0},
    ])
    assert MAGIC not in stubbed
    s = _get()
    assert s["fulcrum"] == 9999.0
    assert s["bias_peak"] == 42.0


def test_reconcile_still_stubs_a_truly_orphaned_magic():
    """No record at all → the original blank-stub path must still fire."""
    other_magic = 770099
    stubbed = ExecBridge.reconcile_from_poll(ACCT, SYM, [
        {"magic": other_magic, "buys": 2, "sells": 0},
    ])
    assert other_magic in stubbed
    s = ExecBridge.get_last_arm(ACCT, SYM, magic=other_magic) or {}
    assert s["active"] is True
    assert s["trigger_kind"] == "recovered"
