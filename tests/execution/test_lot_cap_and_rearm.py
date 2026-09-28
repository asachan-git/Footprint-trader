"""Lot ladder cap (grid_levels.max_lots) and the committed-cycle side-re-arm guard."""
from __future__ import annotations

import pytest

import execution.exec_bridge as eb
import execution.zone_triggers as zt
import server.routes.exec_bridge as route
from execution.exec_bridge import ExecBridge, magic_for
from execution.grid_planner import _build_legs, _ladder
from execution.zone_triggers import Trigger

ACC, SYM = "t1", "XAUUSD+"


def test_ladder_caps_at_max_lot():
    assert _ladder(6, 0.1, 0.1, heavy_near_mid=False, max_lot=0.4) == [0.1, 0.2, 0.3, 0.4, 0.4, 0.4]


def test_ladder_uncapped_when_max_lot_zero():
    assert _ladder(5, 0.01, 0.01, heavy_near_mid=False) == [0.01, 0.02, 0.03, 0.04, 0.05]


def test_build_legs_skew_leg_respects_cap():
    buys, sells = _build_legs(100.0, 5, 1.0, "buy", 0.1, 0.1, max_lot=0.4)
    assert [l.lot for l in buys] == [0.1, 0.2, 0.3, 0.4, 0.4, 0.4]
    assert max(l.lot for l in sells) == 0.4


@pytest.fixture
def bridge(monkeypatch):
    for name in ("persist_arm", "persist_emit", "_emit_exit_audit"):
        monkeypatch.setattr(eb, name, lambda *a, **k: None)
    monkeypatch.setattr(ExecBridge, "_audit", classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(route, "_emit_audit", lambda *a, **k: None)
    ExecBridge.reset()
    ExecBridge.set_quote(ACC, SYM, 4000.0, 4000.2)
    yield
    ExecBridge.reset()


@pytest.mark.parametrize("kind,fn,trigger_fn", [
    ("hvn_inside_touch", "_touch_arm_tf", "touch_arm_trigger"),
    ("lvn_edge_touch", "_lvn_touch_arm_tf", "lvn_touch_arm_trigger"),
])
def test_committed_cycle_never_side_rearms(bridge, monkeypatch, kind, fn, trigger_fn):
    """After fullfill (buys committed, sells cancelled/closed) a fresh edge touch must not
    rebuild the sell ladder."""
    trig = Trigger(kind=kind, fulcrum_price=4000.0, raw_range=5.0, confidence=0.8,
                   context={"edge": "top", "node_low": 3995.0, "node_high": 4000.0})
    monkeypatch.setattr(zt, trigger_fn, lambda *a, **k: trig)
    monkeypatch.setattr(ExecBridge, "touch_arm_check", classmethod(lambda cls, *a, **k: True))

    def _no_plan(*a, **k):
        raise AssertionError("plan_grid_levels must not run for a committed cycle")
    import execution.grid_planner as gp
    monkeypatch.setattr(gp, "plan_grid_levels", _no_plan)

    mg = magic_for(kind, "15m")
    ExecBridge.set_last_arm(ACC, SYM, magic=mg, active=True, fulcrum=4000.0, be_done_buy=True,
                            be_done_sell=False, buy_n=5, sell_n=5, ts=1.0)
    ExecBridge.set_open(ACC, SYM, 5, 0, magic=mg, buys=5, sells=0)
    settings = {"grid_levels": {"triggers": [{"kind": kind, "tfs": ["15m"]}]},
                "execution": {"symbol_map": {"XAUTUSDT": SYM}}}
    getattr(route, fn)(ACC, SYM, "15m", settings, venue_mid=4000.1)
    assert ExecBridge._seq == []


def test_uncommitted_cycle_still_side_rearms(bridge, monkeypatch):
    """The guard is narrow: a cycle that has NOT full-filled keeps its backfill path."""
    trig = Trigger(kind="hvn_inside_touch", fulcrum_price=4000.0, raw_range=5.0, confidence=0.8,
                   context={"edge": "top"})
    monkeypatch.setattr(zt, "touch_arm_trigger", lambda *a, **k: trig)
    monkeypatch.setattr(ExecBridge, "touch_arm_check", classmethod(lambda cls, *a, **k: True))
    called = []
    import execution.grid_planner as gp
    monkeypatch.setattr(gp, "plan_grid_levels",
                        lambda *a, **k: called.append(1) or gp.GridPlan(verdict="skip", skip_reason="test"))
    mg = magic_for("hvn_inside_touch", "15m")
    ExecBridge.set_last_arm(ACC, SYM, magic=mg, active=True, fulcrum=4000.0, be_done_buy=False,
                            be_done_sell=False, buy_n=5, sell_n=5, ts=1.0)
    ExecBridge.set_open(ACC, SYM, 2, 0, magic=mg, buys=2, sells=0)
    settings = {"grid_levels": {"triggers": [{"kind": "hvn_inside_touch", "tfs": ["15m"]}]},
                "execution": {"symbol_map": {"XAUTUSDT": SYM}}}
    route._touch_arm_tf(ACC, SYM, "15m", settings, venue_mid=4000.1)
    assert called == [1]
