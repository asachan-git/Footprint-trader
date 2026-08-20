"""The bias trail: the exit path 45% of all logged cycles go through.

Three properties, each pinned to a way this mechanism has actually failed:

  fires at all          08b76a7 shipped `side_pnl <= peak * (gb/100)` against a
                        COMBINED peak. Bias is chosen as the larger side, so that
                        inequality has no solution in any reachable state — the
                        trail could not book anything, and it survived until the
                        branch was abandoned. The production signature was an
                        exit-reason counter reading zero, which is indistinguishable
                        from a quiet market. Only a test catches this.
  survives partial fill the original gate demanded every leg of a side open at once,
                        so one leg hitting its TP ceiling made the condition false
                        forever (3ad9740).
  never books a loss    give-back-off-peak has no zero floor, so a side that peaked
                        then ran deep negative still "retraced >= giveback%" and
                        booked — observed at -1895 off a peak of 1500 — while also
                        setting bias_booked and retiring the trail.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge, CLOSE_SIDE, MOVE_BE

CFG = {"grid_levels": {
    "bias_trail_enabled": True,
    "bias_trail_activate_usd": 100.0,
    "bias_trail_giveback_pct": 40.0,
    "bias_book_frac": 0.5,
    "cycle_net_target_usd": 0.0,      # keep other exits out of the way
    "cycle_max_loss_usd": 0.0,
    "cycle_close_on_full_hedge": False,
    "fullfill_be_enabled": False,
}}


def _arm(acct, *, buy_n=4, sell_n=4, magic=770013):
    ExecBridge.set_last_arm(acct, "XAUUSD.pc", magic=magic, active=True,
                            buy_n=buy_n, sell_n=sell_n, tf="15m")


def _poll(acct, *, buys, sells, buy_pnl, sell_pnl, magic=770013):
    return ExecBridge.monitor_cycle(acct, "XAUUSD.pc", CFG, pnl=buy_pnl + sell_pnl,
                                    buys=buys, sells=sells, tf="15m", magic=magic,
                                    buy_pnl=buy_pnl, sell_pnl=sell_pnl)


def test_trigger_is_satisfiable():
    """A peak of 500 then a retrace to 250 is a 50% give-back against a 40%
    threshold. If this does not fire, the condition has no solution — which is
    exactly the state 08b76a7 shipped."""
    _arm("TRAIL1")
    assert _poll("TRAIL1", buys=4, sells=0, buy_pnl=500.0, sell_pnl=0.0) is None
    assert _poll("TRAIL1", buys=4, sells=0, buy_pnl=250.0, sell_pnl=0.0) == "bias_book_trail"


def test_books_a_fraction_and_moves_the_rest_to_breakeven():
    """The runner is the point: book part, keep the rest risk-free."""
    _arm("TRAIL2")
    _poll("TRAIL2", buys=4, sells=0, buy_pnl=500.0, sell_pnl=0.0)
    _poll("TRAIL2", buys=4, sells=0, buy_pnl=250.0, sell_pnl=0.0)
    types = [c.get("type") for c in ExecBridge.poll("TRAIL2")]
    assert CLOSE_SIDE in types
    assert MOVE_BE in types


def test_holds_while_still_above_the_giveback_threshold():
    _arm("TRAIL3")
    _poll("TRAIL3", buys=4, sells=0, buy_pnl=500.0, sell_pnl=0.0)
    assert _poll("TRAIL3", buys=4, sells=0, buy_pnl=400.0, sell_pnl=0.0) is None


def test_does_not_arm_below_the_activation_floor():
    _arm("TRAIL4")
    _poll("TRAIL4", buys=4, sells=0, buy_pnl=50.0, sell_pnl=0.0)
    assert _poll("TRAIL4", buys=4, sells=0, buy_pnl=10.0, sell_pnl=0.0) is None


def test_survives_a_partial_fill_after_the_peak_is_set():
    """3ad9740: a leg closes, count drops below buy_n, and the pre-fix gate read
    false forever — the survivors were left with no exit but a distant TP."""
    _arm("TRAIL5")
    assert _poll("TRAIL5", buys=4, sells=0, buy_pnl=500.0, sell_pnl=0.0) is None
    # one leg hit its TP ceiling and closed: 3 of 4 left
    assert _poll("TRAIL5", buys=3, sells=0, buy_pnl=250.0, sell_pnl=0.0) == "bias_book_trail"


def test_a_side_with_no_positions_is_never_the_bias():
    _arm("TRAIL6")
    assert _poll("TRAIL6", buys=0, sells=0, buy_pnl=0.0, sell_pnl=0.0) is None


def test_never_books_a_loss_as_a_profit_lock():
    """Peak 1500 then -1895 satisfies 'retraced >= 40% off peak' on its own. The
    floor must reject it — booking there realizes a reversal AND sets bias_booked,
    retiring the trail for the rest of the cycle."""
    _arm("TRAIL7")
    assert _poll("TRAIL7", buys=4, sells=0, buy_pnl=1500.0, sell_pnl=0.0) is None
    assert _poll("TRAIL7", buys=4, sells=0, buy_pnl=-1895.0, sell_pnl=0.0) != "bias_book_trail"


def test_trail_still_available_after_a_rejected_loss_book():
    """The cycle recovers to a fresh give-back; the trail must still fire."""
    _arm("TRAIL8")
    _poll("TRAIL8", buys=4, sells=0, buy_pnl=1500.0, sell_pnl=0.0)
    _poll("TRAIL8", buys=4, sells=0, buy_pnl=-1895.0, sell_pnl=0.0)
    assert _poll("TRAIL8", buys=4, sells=0, buy_pnl=600.0, sell_pnl=0.0) == "bias_book_trail"


# ── partial-fill tracking (bias_trail_track_partial) ─────────────────────────
# The gap this closes was found live, not by a test: magic 770052 on 2026-08-20
# had buy_n=2 with 1 leg filled, reached +12.6..+31.3 USC at the session high
# against a 5.0 activate threshold, and bias_peak never left 0.0 — the trail
# block was skipped entirely because no side was "fully filled".

CFG_PARTIAL = {"grid_levels": {**CFG["grid_levels"], "bias_trail_track_partial": True}}
CFG_STRICT = {"grid_levels": {**CFG["grid_levels"], "bias_trail_track_partial": False}}


def _poll_cfg(acct, cfg, *, buys, sells, buy_pnl, sell_pnl, magic=770013):
    return ExecBridge.monitor_cycle(acct, "XAUUSD.pc", cfg, pnl=buy_pnl + sell_pnl,
                                    buys=buys, sells=sells, tf="15m", magic=magic,
                                    buy_pnl=buy_pnl, sell_pnl=sell_pnl)


def test_partial_side_in_profit_is_tracked_when_enabled():
    """1 of 2 legs filled, side in profit, then a 50%+ giveback must book."""
    _arm("PART1", buy_n=2, sell_n=2)
    assert _poll_cfg("PART1", CFG_PARTIAL, buys=1, sells=0,
                     buy_pnl=500.0, sell_pnl=0.0) is None          # peak recorded
    assert _poll_cfg("PART1", CFG_PARTIAL, buys=1, sells=0,
                     buy_pnl=200.0, sell_pnl=0.0) == "bias_book_trail"


def test_partial_side_is_invisible_when_disabled():
    """Historical behaviour: the trail never sees a partially filled winner."""
    _arm("PART2", buy_n=2, sell_n=2)
    assert _poll_cfg("PART2", CFG_STRICT, buys=1, sells=0,
                     buy_pnl=500.0, sell_pnl=0.0) is None
    assert _poll_cfg("PART2", CFG_STRICT, buys=1, sells=0,
                     buy_pnl=200.0, sell_pnl=0.0) is None          # still nothing


def test_partial_side_at_a_loss_is_not_tracked():
    """Only a side in PROFIT qualifies — a losing partial must not arm a trail."""
    _arm("PART3", buy_n=2, sell_n=2)
    assert _poll_cfg("PART3", CFG_PARTIAL, buys=1, sells=0,
                     buy_pnl=-500.0, sell_pnl=0.0) is None


def test_full_ladder_still_works_with_the_flag_on():
    _arm("PART4", buy_n=2, sell_n=2)
    assert _poll_cfg("PART4", CFG_PARTIAL, buys=2, sells=0,
                     buy_pnl=500.0, sell_pnl=0.0) is None
    assert _poll_cfg("PART4", CFG_PARTIAL, buys=2, sells=0,
                     buy_pnl=200.0, sell_pnl=0.0) == "bias_book_trail"
