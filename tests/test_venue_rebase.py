"""Analysis -> venue is a SHIFT, not a scale.

Both feeds quote the same metal with a roughly constant spread between them, so
the map between frames is additive. A ratio distorts every distance by that
ratio and — worse — moves each leg by a different amount the further it sits
from the anchor, so the fulcrum lands off the drawn zone edge and the ladder
comes out subtly asymmetric around it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.grid_planner import GridPlan, Leg, _rebase_to_venue

ANALYSIS, VENUE = 4000.0, 4005.21          # a +5.21 venue premium
DELTA = VENUE - ANALYSIS


def _plan():
    return GridPlan(verdict="arm", fulcrum=4000.0, step=2.0,
                    buy_legs=[Leg(4002.0, 0.25), Leg(4004.0, 0.50)],
                    sell_legs=[Leg(3998.0, 0.25), Leg(3996.0, 0.50)],
                    buy_tp=4040.0, sell_tp=3960.0)


def test_prices_shift_by_the_delta():
    p = _rebase_to_venue(_plan(), ANALYSIS, VENUE)
    assert p.fulcrum == round(4000.0 + DELTA, 4)
    assert [l.price for l in p.buy_legs] == [round(4002.0 + DELTA, 4), round(4004.0 + DELTA, 4)]
    assert [l.price for l in p.sell_legs] == [round(3998.0 + DELTA, 4), round(3996.0 + DELTA, 4)]
    assert p.buy_tp == round(4040.0 + DELTA, 4)
    assert p.sell_tp == round(3960.0 + DELTA, 4)


def test_step_is_a_distance_and_does_not_move():
    """Under a ratio this became 2.0026 — small, but it desynchronises the
    server's ladder geometry from the prices actually resting at the broker."""
    assert _rebase_to_venue(_plan(), ANALYSIS, VENUE).step == 2.0


def test_leg_spacing_survives_the_transform():
    """The ratio bug's real damage: legs moved by different amounts, so the
    ladder stopped being evenly spaced around the fulcrum."""
    p = _rebase_to_venue(_plan(), ANALYSIS, VENUE)
    ups = [l.price for l in p.buy_legs]
    downs = [l.price for l in p.sell_legs]
    assert round(ups[1] - ups[0], 6) == 2.0
    assert round(downs[0] - downs[1], 6) == 2.0
    assert round(ups[0] - p.fulcrum, 6) == 2.0
    assert round(p.fulcrum - downs[0], 6) == 2.0


def test_identity_when_the_frames_agree():
    p = _rebase_to_venue(_plan(), ANALYSIS, ANALYSIS)
    assert p.rebased is False
    assert p.fulcrum == 4000.0


def test_round_trip_returns_the_analysis_price():
    p = _rebase_to_venue(_plan(), ANALYSIS, VENUE)
    assert round(p.fulcrum - DELTA, 4) == 4000.0


def test_zero_targets_stay_zero():
    """A 0 TP/SL means 'none' — a shift must not turn it into a real price."""
    plan = _plan()
    plan.buy_tp = plan.sell_tp = plan.buy_sl = plan.sell_sl = 0.0
    p = _rebase_to_venue(plan, ANALYSIS, VENUE)
    assert (p.buy_tp, p.sell_tp, p.buy_sl, p.sell_sl) == (0.0, 0.0, 0.0, 0.0)
