"""atr_from_store must not let weekend bars (Binance's XAUT feed keeps posting thin
Sat/Sun drift while real gold is closed) contaminate a session-anchored symbol's ATR.

Regression for 2026-08-03: a Monday-morning grid arm computed step=0.36 (should be
2-3.6) because its ATR(14) window was entirely Sunday 06:30-07:30 bars, ranges 0.4-3.4.
Same underlying bug pipeline.features.vp_cache's weekend-skip (255bcbd, 2026-06-29)
fixed for the VP/session-history path; this locks in the equivalent fix for ATR."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pipeline.state_store as ss
from pipeline.features.atr import atr_from_store


def _bar(dt: datetime, h: float, l: float, c: float):
    return NS(close_ts=int(dt.timestamp()), ohlc=NS(o=c, h=h, l=l, c=c))


class _FakeStore:
    def __init__(self, bars):
        self._bars = bars

    def recent(self, symbol, tf, n):
        return self._bars[-n:]


def _patch_store(monkeypatch, bars):
    monkeypatch.setattr(ss, "store", lambda: _FakeStore(bars))


def test_atr_skips_weekend_bars_for_gold(monkeypatch):
    # Friday: real trading, wide bars (range ~10). Sat/Sun: thin synthetic drift
    # (range ~0.5) that would drag ATR near zero if counted.
    friday = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)  # Friday
    bars = []
    px = 4000.0
    for i in range(20):
        dt = friday + timedelta(minutes=15 * i)
        bars.append(_bar(dt, px + 10, px, px + 5))
        px += 5
    weekend_start = friday + timedelta(days=1, hours=6)  # Saturday
    for i in range(40):
        dt = weekend_start + timedelta(minutes=15 * i)
        bars.append(_bar(dt, px + 0.5, px, px + 0.2))
        px += 0.1
    bars.sort(key=lambda b: b.close_ts)
    _patch_store(monkeypatch, bars)

    a = atr_from_store("XAUTUSDT", "15m", period=14)
    assert a > 8.0, f"weekend drift leaked into gold's ATR window: got {a}"


def test_atr_keeps_weekend_bars_for_btc(monkeypatch):
    # BTC genuinely trades weekends — its Sat/Sun bars must NOT be filtered.
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    bars = []
    px = 60000.0
    for i in range(20):
        dt = saturday + timedelta(minutes=15 * i)
        bars.append(_bar(dt, px + 100, px, px + 50))
        px += 50
    _patch_store(monkeypatch, bars)

    a = atr_from_store("BTCUSDT", "15m", period=14)
    assert a > 50.0, f"BTC's real weekend bars got incorrectly filtered: got {a}"


def test_atr_sentinel_bar_still_dropped(monkeypatch):
    # Pre-existing guard (unrelated to this fix) must still work: a forming/sentinel
    # bar (close_ts >= 9_000_000_000) must never enter the true-range calc.
    friday = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(friday + timedelta(minutes=15 * i), 4005, 4000, 4002) for i in range(15)]
    bars.append(NS(close_ts=9_999_999_999, ohlc=NS(o=4002, h=4130, l=4002, c=4130)))
    _patch_store(monkeypatch, bars)

    a = atr_from_store("XAUTUSDT", "15m", period=14)
    assert a < 10.0, f"sentinel bar poisoned the ATR: got {a}"
