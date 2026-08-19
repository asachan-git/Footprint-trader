"""The bounded-loss floor: per-leg disaster stop and the per-cycle loss cap.

Both are absent from the Jun-24 base by construction, so these tests are the
regression guard for the two ways that absence used to show up.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge


class _Leg:
    def __init__(self, price, lot):
        self.price, self.lot = price, lot


class _Plan:
    """Minimal stand-in for a rebased GridPlan."""
    def __init__(self, buy_sl=0.0, sell_sl=0.0):
        self.buy_legs = [_Leg(4050.0, 0.25), _Leg(4053.5, 0.50)]
        self.sell_legs = [_Leg(4043.0, 0.25), _Leg(4039.5, 0.50)]
        self.buy_tp, self.sell_tp = 4090.0, 4010.0
        self.buy_sl, self.sell_sl = buy_sl, sell_sl
        self.trigger_kind = "hvn_inside_touch"
        self.trigger_context = {}


def test_leg_sl_is_measured_from_each_legs_own_entry():
    """A shared per-side level would put the outer leg a full ladder-span from
    its stop while the inner sits on top of it; per-leg keeps risk identical."""
    assert ExecBridge._leg_sl(4050.0, "buy", 20.0) == 4030.0
    assert ExecBridge._leg_sl(4053.5, "buy", 20.0) == 4033.5
    assert ExecBridge._leg_sl(4043.0, "sell", 20.0) == 4063.0


def test_leg_sl_disabled_by_zero_and_never_negative():
    assert ExecBridge._leg_sl(4050.0, "buy", 0.0) == 0.0
    assert ExecBridge._leg_sl(10.0, "buy", 20.0) == 0.0     # would go through zero


def test_every_leg_carries_a_stop_when_disaster_is_set():
    """The defect this closes: hvn_inside_touch and hvn_edge always went out
    with sl=0.0, so only 15.7% of live positions ever had a broker stop."""
    cmds = ExecBridge.enqueue_grid_plan("T", "XAUUSD.pc", _Plan(), close_first=False,
                                        magic=770013, disaster_sl_usd=20.0)
    legs = [c for c in cmds if getattr(c, "type", "") == "PLACE_PENDING"]
    assert len(legs) == 4
    assert all(c.sl > 0 for c in legs), "every leg must carry a stop"
    buys = [c for c in legs if c.order_type == "buy_stop"]
    assert all(c.sl < c.price for c in buys), "a buy stop's SL sits below its entry"
    sells = [c for c in legs if c.order_type == "sell_stop"]
    assert all(c.sl > c.price for c in sells), "a sell stop's SL sits above its entry"


def test_structural_sl_still_wins():
    """Displacement sets a candle-extreme SL; the disaster stop must not override it."""
    cmds = ExecBridge.enqueue_grid_plan("T2", "XAUUSD.pc", _Plan(buy_sl=4001.0),
                                        close_first=False, magic=770013,
                                        disaster_sl_usd=20.0)
    buys = [c for c in cmds
            if getattr(c, "type", "") == "PLACE_PENDING" and c.order_type == "buy_stop"]
    assert all(c.sl == 4001.0 for c in buys)


def test_no_stop_when_feature_is_off():
    cmds = ExecBridge.enqueue_grid_plan("T3", "XAUUSD.pc", _Plan(), close_first=False,
                                        magic=770013, disaster_sl_usd=0.0)
    legs = [c for c in cmds if getattr(c, "type", "") == "PLACE_PENDING"]
    assert all(c.sl == 0.0 for c in legs)
