"""Build OHLCV bars from Dhan LTP ticks (REST or WS feed).

Since Dhan doesn't expose individual trades with aggressor side, bars are
LTP-based OHLCV only — no bid/ask flow, no true footprint delta.
The payload format is "dhan_v1" so the normalizer can handle it correctly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

LOG = logging.getLogger(__name__)

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


def _bar_close_ts(tick_ts_s: int, tf_s: int) -> int:
    return ((tick_ts_s // tf_s) + 1) * tf_s


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    h = hashlib.sha1(f"{symbol}|{tf}|{close_ts}".encode()).hexdigest()[:16]
    return f"{symbol}|{tf}|{close_ts}|{h}"


@dataclass
class _AccumBar:
    symbol: str
    tf: str
    close_ts: int
    open: float = 0.0
    high: float = float("-inf")
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    ticks: int = 0

    def add(self, price: float, volume: float) -> None:
        if self.ticks == 0:
            self.open = price
        self.close = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.volume += volume
        self.ticks += 1

    def to_payload(self, price_step: float = 0.05) -> dict:
        """Return dhan_v1 payload compatible with /ingest endpoint."""
        if self.ticks == 0:
            return {}
        # Build minimal bid/ask ladders (no order flow — volume spread evenly)
        prices = sorted({
            round(p / price_step) * price_step
            for p in [self.open, self.high, self.low, self.close]
        })
        n = len(prices) or 1
        vol_per_level = self.volume / n
        bid_ladder = [{"price": p, "vol": vol_per_level} for p in prices]
        ask_ladder = [{"price": p, "vol": 0.0} for p in prices]
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "close_ts": self.close_ts,
            "bar_id": _bar_id(self.symbol, self.tf, self.close_ts),
            "ohlc": {
                "o": self.open,
                "h": self.high,
                "l": self.low,
                "c": self.close,
            },
            "volume": self.volume,
            "bid": bid_ladder,
            "ask": ask_ladder,
            "delta": 0.0,   # unknown without trade-level aggressor data
            "format": "dhan_v1",
        }


class BarBuilder:
    """Accumulate (ts_ms, ltp, volume) ticks into closed OHLCV bars."""

    def __init__(
        self,
        symbol: str,
        tf: str,
        on_bar_close: Callable[[dict], None],
        price_step: float = 0.05,
    ) -> None:
        if tf not in TF_SECONDS:
            raise ValueError(f"Unsupported tf {tf!r}. Must be one of {list(TF_SECONDS)}")
        self._symbol = symbol
        self._tf = tf
        self._tf_s = TF_SECONDS[tf]
        self._on_bar_close = on_bar_close
        self._price_step = price_step
        self._current: _AccumBar | None = None

    def on_tick(self, ts_ms: int, ltp: float, volume: float = 0.0) -> None:
        ts_s = ts_ms // 1000
        bar_close_ts = _bar_close_ts(ts_s, self._tf_s)

        if self._current is None:
            self._current = _AccumBar(self._symbol, self._tf, bar_close_ts)

        if bar_close_ts > self._current.close_ts:
            payload = self._current.to_payload(self._price_step)
            if payload:
                self._on_bar_close(payload)
                LOG.info(f"[ltp_bar] {payload['bar_id']} o={payload['ohlc']['o']} c={payload['ohlc']['c']} vol={payload['volume']:.0f}")
            self._current = _AccumBar(self._symbol, self._tf, bar_close_ts)

        self._current.add(ltp, volume)

    def flush(self) -> None:
        """Force-emit current partial bar (e.g. on shutdown)."""
        if self._current and self._current.ticks > 0:
            payload = self._current.to_payload(self._price_step)
            if payload:
                self._on_bar_close(payload)
            self._current = None
