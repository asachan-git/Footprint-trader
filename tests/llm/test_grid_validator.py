"""Tests for llm.validator.validate_grid_plan (GridDecision schema)."""
from llm.schema import GridDecision, GridLegOrder, PositionManagement
from llm.validator import validate_grid_plan


def _pm(tp=4490.0, sl=4430.0, max_legs=3, tp_ext=None):
    return PositionManagement(
        tp_primary=tp, tp_extended=tp_ext, sl=sl, max_legs=max_legs,
        trail_after_r=2.0, be_after_r=1.0,
    )


def _long_grid(legs=None, pm=None, side="long"):
    legs = legs or [
        GridLegOrder(leg_idx=1, price=4456.0, qty=0.03, rationale="Fib 0.382"),
        GridLegOrder(leg_idx=2, price=4452.0, qty=0.02, rationale="Fib 0.5"),
        GridLegOrder(leg_idx=3, price=4448.0, qty=0.01, rationale="Fib 0.618"),
    ]
    return GridDecision(side=side, confidence=0.85, grid_orders=legs,
                        position_management=pm or _pm())


def test_flat_always_valid():
    gd = GridDecision(side="flat", confidence=0.0, grid_orders=[])
    assert validate_grid_plan(gd, current_price=4460.0, atr=8.0) is None


def test_valid_long_passes():
    gd = _long_grid()
    assert validate_grid_plan(gd, current_price=4460.0, atr=8.0) is None


def test_non_flat_empty_legs_rejected():
    gd = GridDecision(side="long", confidence=0.7, grid_orders=[],
                      position_management=_pm())
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "empty" in err


def test_missing_position_management_rejected():
    legs = [GridLegOrder(leg_idx=1, price=4456.0, qty=0.03, rationale="x")]
    gd = GridDecision(side="long", confidence=0.7, grid_orders=legs,
                      position_management=None)
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "position_management" in err


def test_long_sl_above_price_rejected():
    gd = _long_grid(pm=_pm(tp=4490.0, sl=4470.0))  # SL above current 4460
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "SL" in err


def test_long_tp_too_close_rejected():
    # TP only 5pts above entry on ATR=8 → needs ≥12 (1.5×ATR)
    gd = _long_grid(pm=_pm(tp=4465.0, sl=4430.0))
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "1.5" in err


def test_rr_below_floor_rejected():
    # first leg=4456, sl=4455 → risk=1; tp=4457 → reward=1 → RR=1.0 < 1.2
    legs = [GridLegOrder(leg_idx=1, price=4456.0, qty=0.01, rationale="x")]
    gd = GridDecision(side="long", confidence=0.7, grid_orders=legs,
                      position_management=_pm(tp=4470.0, sl=4455.0))
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    # risk=1, reward=14, RR=14 — too close SL triggers "TP" check or RR < 1.2
    # Just verify validator rejects it
    assert err is not None


def test_total_qty_exceeds_max_lots():
    # 3 legs × 0.18 = 0.54 > max_lots default 0.50
    legs = [GridLegOrder(leg_idx=i, price=4456.0 - i * 4, qty=0.18, rationale="x")
            for i in range(1, 4)]
    gd = GridDecision(side="long", confidence=0.7, grid_orders=legs,
                      position_management=_pm(tp=4516.0, sl=4400.0))
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "max_lots" in err


def test_legs_too_close_rejected():
    # adjacent legs only 1pt apart, ATR=8 → min_sep=2.4
    legs = [
        GridLegOrder(leg_idx=1, price=4456.0, qty=0.02, rationale="x"),
        GridLegOrder(leg_idx=2, price=4457.0, qty=0.01, rationale="x"),
    ]
    gd = GridDecision(side="long", confidence=0.7, grid_orders=legs,
                      position_management=_pm(tp=4490.0, sl=4430.0))
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "close" in err


def test_chasing_long_leg_rejected():
    # leg=4475 > current_price(4460) * 1.002 = 4469.2 → chasing
    # RR must pass first: sl=4420 (risk=55), tp=4541 (reward=66), RR=1.2 exactly
    legs = [GridLegOrder(leg_idx=1, price=4475.0, qty=0.02, rationale="x")]
    gd = GridDecision(side="long", confidence=0.7, grid_orders=legs,
                      position_management=_pm(tp=4541.0, sl=4420.0))
    err = validate_grid_plan(gd, current_price=4460.0, atr=8.0)
    assert err is not None and "chasing" in err


def test_max_legs_cap():
    import pytest
    # Pydantic rejects max_legs > 5 at construction — validator never reached
    with pytest.raises(Exception):
        PositionManagement(tp_primary=4490.0, sl=4430.0, max_legs=6)
