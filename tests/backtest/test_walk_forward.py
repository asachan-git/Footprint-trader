from backtest.walk_forward import label
from pipeline.types import Bar, OHLC


def _b(close_ts, h, l, c=None):
    return Bar(
        bar_id=f"NQ|1m|{close_ts}", symbol="NQ", tf="1m", close_ts=close_ts, source="live",
        ohlc=OHLC(o=100.0, h=h, l=l, c=c if c is not None else (h + l) / 2),
        bid_ladder=(), ask_ladder=(),
    )


def test_long_hits_tp():
    bars = [_b(1, 102, 100), _b(2, 105, 101), _b(3, 110, 104)]
    o = label("NQ|1m|0", "long", entry=100, sl=98, tp=104, forward=bars)
    assert o.hit == "tp"
    assert o.realized_r == 2.0   # (104-100)/(100-98)


def test_short_hits_sl():
    bars = [_b(1, 101, 100), _b(2, 103, 100.5)]
    o = label("NQ|1m|0", "short", entry=100, sl=102, tp=94, forward=bars)
    assert o.hit == "sl"
    assert o.realized_r == -1.0


def test_expire():
    bars = [_b(i, 100.5, 99.5) for i in range(5)]
    o = label("NQ|1m|0", "long", entry=100, sl=95, tp=110, forward=bars, max_lookahead=5)
    assert o.hit == "expire"
