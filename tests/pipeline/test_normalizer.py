from pipeline.normalizer import normalize


def test_atas_v1_full_ladder():
    bar = normalize({
        "format": "atas_v1",
        "source": "live",
        "bar_id": "GC|1m|1000",
        "symbol": "GC",
        "tf": "1m",
        "close_ts": 1000,
        "ohlc": {"o": 3320.0, "h": 3325.0, "l": 3318.0, "c": 3323.5},
        "bid_ladder": [{"price": 3318.0, "vol": 45}, {"price": 3319.0, "vol": 23}],
        "ask_ladder": [{"price": 3318.0, "vol": 12}, {"price": 3319.0, "vol": 67}],
        "delta": 234,
        "poc": 3321.0,
    })
    assert bar.symbol == "GC"
    assert bar.bid_ladder[0].vol == 45
    assert bar.ask_ladder[1].price == 3319.0
    assert bar.delta == 234


def test_atas_v1_empty_ladder():
    bar = normalize({
        "format": "atas_v1",
        "source": "live",
        "bar_id": "GC|1m|2000",
        "symbol": "GC",
        "tf": "1m",
        "close_ts": 2000,
        "ohlc": {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
        "bid_ladder": [],
        "ask_ladder": [],
        "delta": 0,
    })
    assert bar.bid_ladder == ()
    assert bar.source == "live"
