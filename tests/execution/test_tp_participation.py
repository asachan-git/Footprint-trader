"""Tests for pipeline.features.tp_participation."""
import pytest
from pipeline.features.tp_participation import compute, adjust_tp, cleanup, _hysteresis
from pipeline.types import Bar, OHLC


def _bar(delta: float, close: float = 4460.0, i: int = 0) -> Bar:
    return Bar(
        bar_id=f"b{i}", symbol="XAUTUSDT", tf="1m", close_ts=1000 + i,
        source="live",
        ohlc=OHLC(o=close - 1, h=close + 2, l=close - 2, c=close),
        bid_ladder=(), ask_ladder=(),
        delta=delta,
    )


def _bars(deltas, close=4460.0):
    return [_bar(d, close, i) for i, d in enumerate(deltas)]


def test_positive_cvd_long_gives_positive_score():
    bars = _bars([100, 80, 120, 90, 110] * 4)
    ps = compute("long", bars, current_tp=4480.0, avg_entry=4456.0, atr_val=8.0)
    assert ps.score > 0.0


def test_negative_cvd_long_gives_negative_score():
    bars = _bars([-100, -80, -120, -90, -110] * 4)
    ps = compute("long", bars, current_tp=4480.0, avg_entry=4456.0, atr_val=8.0)
    assert ps.score < 0.0


def test_score_bounded():
    bars = _bars([500] * 20)
    ps = compute("long", bars, current_tp=4480.0, avg_entry=4456.0, atr_val=8.0)
    assert -1.0 <= ps.score <= 1.0


def test_adjust_tp_extends_on_high_score(monkeypatch):
    """High participation (score ≥ 0.5) → TP should extend."""
    pid = "test_extend_001"
    cleanup(pid)
    daily_vp = {"poc": 4455.0, "vah": 4490.0, "val": 4440.0,
                "hvn": [{"low": 4484.0, "high": 4488.0}]}
    bars = _bars([200, 180, 220, 190, 210] * 4)
    current_tp = 4476.0
    new_tp, reason = adjust_tp(
        position_id=pid, side="long", bars=bars,
        current_tp=current_tp, avg_entry=4456.0, atr_val=8.0,
        daily_vp=daily_vp,
    )
    # Either extended or held — confirm no shrink on positive flow
    assert new_tp >= current_tp, f"Expected TP >= {current_tp}, got {new_tp} ({reason})"
    cleanup(pid)


def test_adjust_tp_shrinks_on_immediate_absorption(monkeypatch):
    """With strong negative CVD right at TP, should shrink."""
    pid = "test_shrink_001"
    cleanup(pid)
    daily_vp = {"poc": 4455.0, "vah": 4490.0, "val": 4440.0,
                "hvn": [{"low": 4470.0, "high": 4474.0}]}
    bars = _bars([-300, -250, -280, -320, -290] * 4, close=4475.0)
    current_tp = 4476.0
    new_tp, reason = adjust_tp(
        position_id=pid, side="long", bars=bars,
        current_tp=current_tp, avg_entry=4456.0, atr_val=8.0,
        daily_vp=daily_vp,
    )
    assert new_tp <= current_tp, f"Expected TP shrink, got {new_tp} ({reason})"
    cleanup(pid)


def test_adjust_tp_hysteresis_no_double_shift():
    """Two consecutive calls within LOCK_BARS should not shift twice."""
    pid = "test_hysteresis_001"
    cleanup(pid)
    daily_vp = {"poc": 4455.0, "vah": 4500.0, "val": 4440.0, "hvn": []}
    bars = _bars([200] * 20)
    tp1, _ = adjust_tp(pid, "long", bars, 4476.0, 4456.0, 8.0, daily_vp)
    tp2, _ = adjust_tp(pid, "long", bars, tp1, 4456.0, 8.0, daily_vp)
    # Second call within lock window → TP should not shift again
    assert tp2 == tp1
    cleanup(pid)


def test_empty_bars_returns_unchanged_tp():
    pid = "test_empty_001"
    cleanup(pid)
    new_tp, _ = adjust_tp(pid, "long", [], 4476.0, 4456.0, 8.0, None)
    assert new_tp == 4476.0
    cleanup(pid)
