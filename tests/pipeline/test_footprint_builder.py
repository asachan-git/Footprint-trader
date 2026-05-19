from bybit.footprint_builder import FootprintBuilder


def test_accumulates_and_emits_on_bar_close():
    emitted = []
    b = FootprintBuilder("BTCUSDT", "1m", on_bar_close=emitted.append, price_step=1.0)

    # First minute: trades from ts 60000-119999 → close_ts 120
    b.on_tick(60_000, 67000.4, 0.5, "Buy")   # bucket 67000
    b.on_tick(70_000, 67001.6, 0.3, "Sell")  # bucket 67002
    b.on_tick(110_000, 67000.0, 0.2, "Sell") # bucket 67000

    # Tick in next minute → triggers emit of the first bar
    b.on_tick(125_000, 67005.0, 0.1, "Buy")

    assert len(emitted) == 1
    bar = emitted[0]
    assert bar["format"] == "bybit_v1"
    assert bar["close_ts"] == 120
    assert bar["trades"] == 3
    assert bar["ohlc"]["o"] == 67000.0
    assert bar["ohlc"]["c"] == 67000.0  # last trade in bar was 67000
    assert bar["delta"] == 0.5 - 0.5    # ask 0.5 (Buy) - bid 0.5 (Sell sum)
    bid_total = sum(l["vol"] for l in bar["bid_ladder"])
    ask_total = sum(l["vol"] for l in bar["ask_ladder"])
    assert bid_total == 0.5
    assert ask_total == 0.5
