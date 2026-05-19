from pipeline.normalizer import normalize
from exness.footprint_builder import FootprintBuilder, infer_side, TICK_FLAG_BUY, TICK_FLAG_SELL


def test_exness_v1_parses():
    bar = normalize({
        "format": "exness_v1",
        "source": "live",
        "bar_id": "XAUUSD|1m|1747300000|abc",
        "symbol": "XAUUSD",
        "tf": "1m",
        "close_ts": 1747300000,
        "ohlc": {"o": 2350.5, "h": 2351.2, "l": 2350.0, "c": 2351.0},
        "bid_ladder": [{"price": 2350.0, "vol": 12.0}],
        "ask_ladder": [{"price": 2351.0, "vol": 8.5}],
        "delta": -3.5,
    })
    assert bar.symbol == "XAUUSD"
    assert bar.bid_ladder[0].vol == 12.0
    assert bar.delta == -3.5


def test_infer_side_from_flags():
    assert infer_side(2350.5, 2350.0, 2351.0, TICK_FLAG_BUY) == "Buy"
    assert infer_side(2350.5, 2350.0, 2351.0, TICK_FLAG_SELL) == "Sell"


def test_infer_side_lee_ready():
    # No flag set; use Lee-Ready
    assert infer_side(2351.0, 2350.0, 2351.0, 0) == "Buy"   # price >= ask
    assert infer_side(2350.0, 2350.0, 2351.0, 0) == "Sell"  # price <= bid
    assert infer_side(2350.7, 2350.0, 2351.0, 0) == "Buy"   # above mid
    assert infer_side(2350.3, 2350.0, 2351.0, 0) == "Sell"  # below mid


def test_builder_emits_on_bar_boundary():
    emitted = []
    b = FootprintBuilder("XAUUSD", "1m", on_bar_close=emitted.append, price_step=0.1)

    # tick at 60_000 ms → close_ts=120
    b.on_tick(60_000, bid=2350.0, ask=2351.0, last=2350.5, volume=1.0, flags=TICK_FLAG_BUY)
    b.on_tick(70_000, bid=2350.1, ask=2351.1, last=2350.6, volume=2.0, flags=TICK_FLAG_SELL)
    # tick in next bar
    b.on_tick(125_000, bid=2350.2, ask=2351.2, last=2350.7, volume=1.5, flags=TICK_FLAG_BUY)

    assert len(emitted) == 1
    bar = emitted[0]
    assert bar["format"] == "exness_v1"
    assert bar["close_ts"] == 120
    assert bar["trades"] == 2
