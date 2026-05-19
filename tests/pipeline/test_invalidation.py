from pipeline.types import Bar, Level, OHLC
from pipeline.footprint import build
from pipeline.features.invalidation import detect_invalidation, check_tp_absorption


def _bar(ohlc_vals, bid=None, ask=None):
    o, h, l, c = ohlc_vals
    bid = bid or [Level(l, 50)]
    ask = ask or [Level(h, 10)]
    return Bar(
        bar_id="TEST|1m|1000", symbol="TEST", tf="1m", close_ts=1000, source="live",
        ohlc=OHLC(o=o, h=h, l=l, c=c),
        bid_ladder=tuple(bid), ask_ladder=tuple(ask),
    )


def test_long_invalidation_sell_absorption_at_entry():
    # Sell absorption at bar HIGH = entry zone → long invalidated
    # bar high = 100.2, entry = 100.0 — absorption within 0.2% entry zone
    bar = _bar((99, 100.2, 99, 99.3),
               bid=[Level(100.2, 5)],
               ask=[Level(100.2, 95)])   # heavy ask at bar HIGH (100.2)
    fp = build(bar)
    # entry_zone_pct default 0.002 → 100.0 * 0.002 = 0.20 → 100.2 ≤ 100.0 + 0.20
    sig = detect_invalidation(bar, fp, "long", entry=100.0, entry_zone_pct=0.003)
    assert sig is not None
    assert "sell absorption" in sig.reason


def test_long_no_invalidation_normal_bar():
    bar = _bar((100, 102, 99.5, 101.5),
               bid=[Level(100.0, 30)],
               ask=[Level(101.0, 20)])
    fp = build(bar)
    sig = detect_invalidation(bar, fp, "long", entry=100.0)
    assert sig is None


def test_tp_absorption_long():
    # Sell absorption AT bar high near TP = full exit
    tp = 103.0
    bar = _bar((101, 103.0, 101, 103),  # bar high = 103.0 = TP
               bid=[Level(103.0, 5)],
               ask=[Level(103.0, 90)])  # heavy ask at bar high = TP
    fp = build(bar)
    reason = check_tp_absorption(bar, fp, "long", take_profit=tp)
    assert reason is not None
    assert "sell absorption" in reason


def test_tp_absorption_not_triggered_away_from_tp():
    bar = _bar((101, 102, 101, 102),
               bid=[Level(102.0, 5)],
               ask=[Level(102.0, 90)])
    fp = build(bar)
    reason = check_tp_absorption(bar, fp, "long", take_profit=110.0)
    assert reason is None
