from pipeline.footprint import build
from pipeline.types import Bar, Level, OHLC


def _bar() -> Bar:
    return Bar(
        bar_id="NQ|1m|1000",
        symbol="NQ",
        tf="1m",
        close_ts=1000,
        source="live",
        ohlc=OHLC(o=100.0, h=102.0, l=99.0, c=101.5),
        bid_ladder=(Level(99.0, 5), Level(100.0, 20), Level(101.0, 10)),
        ask_ladder=(Level(100.0, 15), Level(101.0, 25), Level(102.0, 8)),
    )


def test_delta_and_poc():
    fp = build(_bar())
    assert fp.total_bid == 35
    assert fp.total_ask == 48
    assert fp.delta == 13
    # POC = price with max total = 100.0 (20+15=35) vs 101.0 (10+25=35) — tie; max() picks first/last
    assert fp.poc_price in {100.0, 101.0}


def test_value_area_contains_poc():
    fp = build(_bar())
    va = fp.value_area(0.7)
    assert va is not None
    lo, hi = va
    assert lo <= fp.poc_price <= hi
