from pipeline.normalizer import normalize


def test_bybit_v1_parses():
    bar = normalize({
        "format": "bybit_v1",
        "source": "live",
        "bar_id": "BTCUSDT|1m|1747300000|abc123",
        "symbol": "BTCUSDT",
        "tf": "1m",
        "close_ts": 1747300000,
        "ohlc": {"o": 67000.0, "h": 67050.0, "l": 66950.0, "c": 67020.0},
        "bid_ladder": [{"price": 66950.0, "vol": 1.23}, {"price": 67000.0, "vol": 0.45}],
        "ask_ladder": [{"price": 67000.0, "vol": 0.88}, {"price": 67050.0, "vol": 2.10}],
        "delta": 1.30,
        "poc": 67000.0,
    })
    assert bar.symbol == "BTCUSDT"
    assert bar.bid_ladder[0].vol == 1.23
    assert bar.ask_ladder[1].price == 67050.0
    assert bar.delta == 1.30
