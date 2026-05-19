from pipeline.normalizer import normalize
from capital.footprint_builder import FootprintBuilder


def test_capital_v1_parses():
    bar = normalize({
        "format": "capital_v1",
        "source": "live",
        "bar_id": "GOLD|1m|1747300000|abc",
        "symbol": "GOLD",
        "tf": "1m",
        "close_ts": 1747300000,
        "ohlc": {"o": 2350.5, "h": 2351.2, "l": 2350.0, "c": 2351.0},
        "bid_ladder": [{"price": 2350.0, "vol": 5.0}],
        "ask_ladder": [{"price": 2351.0, "vol": 7.0}],
        "delta": 2.0,
    })
    assert bar.symbol == "GOLD"
    assert bar.delta == 2.0


def test_capital_tick_rule_emits_on_boundary():
    emitted = []
    b = FootprintBuilder("GOLD", "1m", on_bar_close=emitted.append, price_step=0.1)

    # First quote — primes prev_mid, no emit
    b.on_quote(60_000, bid=2350.0, ask=2350.2)  # mid=2350.1
    # Mid moves up → buyer aggression
    b.on_quote(65_000, bid=2350.1, ask=2350.3)  # mid=2350.2 > 2350.1
    # Mid moves down → seller aggression
    b.on_quote(70_000, bid=2350.0, ask=2350.2)  # mid=2350.1 < 2350.2
    # Cross to next bar
    b.on_quote(125_000, bid=2350.2, ask=2350.4)  # mid=2350.3

    assert len(emitted) == 1
    bar = emitted[0]
    assert bar["format"] == "capital_v1"
    assert bar["close_ts"] == 120
    assert bar["trades"] == 2  # one uptick, one downtick
    assert bar["delta"] == 0.0  # 1 buy - 1 sell
