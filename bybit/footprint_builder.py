"""Accumulate raw Bybit trade ticks into per-bar footprint matrix.

Each tick: {ts_ms, price, size, side} where side='Buy' = taker bought (aggressor on ask),
side='Sell' = taker sold (aggressor on bid).

Buckets by (bar_close_ts, price_level). Emits a canonical-shaped payload (bybit_v1)
when bar boundary crosses.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


def _bar_close_ts(tick_ts_s: int, tf_seconds: int) -> int:
    return ((tick_ts_s // tf_seconds) + 1) * tf_seconds


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    h = hashlib.sha1(f"{symbol}|{tf}|{close_ts}".encode()).hexdigest()[:16]
    return f"{symbol}|{tf}|{close_ts}|{h}"


@dataclass
class _AccumBar:
    symbol: str
    tf: str
    close_ts: int
    bid_at_price: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    ask_at_price: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    first_price: float | None = None
    last_price: float | None = None
    high: float = float("-inf")
    low: float = float("inf")
    trades: int = 0
    # Intra-bar CVD path tracking (real wicks for CVD candle pane).
    cvd_open: float | None = None
    cvd_high: float = float("-inf")
    cvd_low: float = float("inf")
    cvd_close: float = 0.0

    def add(self, price: float, size: float, side: str, running_cvd: float) -> None:
        if self.first_price is None:
            self.first_price = price
            self.cvd_open = running_cvd
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.cvd_high = max(self.cvd_high, running_cvd)
        self.cvd_low = min(self.cvd_low, running_cvd)
        self.cvd_close = running_cvd
        self.trades += 1
        if side == "Buy":
            self.ask_at_price[price] += size
        else:  # "Sell"
            self.bid_at_price[price] += size

    def to_payload(self) -> dict:
        prices = set(self.bid_at_price) | set(self.ask_at_price)
        bid_ladder = [{"price": p, "vol": self.bid_at_price.get(p, 0.0)} for p in sorted(prices)]
        ask_ladder = [{"price": p, "vol": self.ask_at_price.get(p, 0.0)} for p in sorted(prices)]
        total_bid = sum(self.bid_at_price.values())
        total_ask = sum(self.ask_at_price.values())
        delta = total_ask - total_bid
        poc = max(prices, key=lambda p: self.bid_at_price.get(p, 0) + self.ask_at_price.get(p, 0)) if prices else None
        return {
            "format": "bybit_v1",
            "source": "live",
            "bar_id": _bar_id(self.symbol, self.tf, self.close_ts),
            "symbol": self.symbol,
            "tf": self.tf,
            "close_ts": self.close_ts,
            "ohlc": {
                "o": self.first_price or 0.0,
                "h": self.high if self.high != float("-inf") else 0.0,
                "l": self.low if self.low != float("inf") else 0.0,
                "c": self.last_price or 0.0,
            },
            "bid_ladder": bid_ladder,
            "ask_ladder": ask_ladder,
            "delta": delta,
            "buyvolume": total_ask,
            "sellvolume": total_bid,
            "poc": poc,
            "trades": self.trades,
            "cvd_open":  self.cvd_open if self.cvd_open is not None else 0.0,
            "cvd_high":  self.cvd_high if self.cvd_high != float("-inf") else 0.0,
            "cvd_low":   self.cvd_low  if self.cvd_low  != float("inf")  else 0.0,
            "cvd_close": self.cvd_close,
        }


class FootprintBuilder:
    """Maintains a current accumulator bar; on tick crossing the close, emits + rotates."""

    def __init__(self, symbol: str, tf: str, on_bar_close: Callable[[dict], None], price_step: float = 0.0):
        self.symbol = symbol
        self.tf = tf
        self.tf_seconds = TF_SECONDS[tf]
        self.on_bar_close = on_bar_close
        self.price_step = price_step
        self._current: _AccumBar | None = None
        # Running CVD across all bars — feeds intra-bar high/low for CVD wicks.
        self._running_cvd: float = 0.0

    def _bucket_price(self, p: float) -> float:
        if self.price_step <= 0:
            return p
        return round(p / self.price_step) * self.price_step

    def on_tick(self, ts_ms: int, price: float, size: float, side: str) -> None:
        ts_s = ts_ms // 1000
        close_ts = _bar_close_ts(ts_s, self.tf_seconds)

        if self._current is None:
            self._current = _AccumBar(self.symbol, self.tf, close_ts)
        elif close_ts != self._current.close_ts:
            self.on_bar_close(self._current.to_payload())
            self._current = _AccumBar(self.symbol, self.tf, close_ts)

        self._running_cvd += size if side == "Buy" else -size
        self._current.add(self._bucket_price(price), size, side, self._running_cvd)

    def flush(self) -> None:
        if self._current is not None and self._current.trades > 0:
            self.on_bar_close(self._current.to_payload())
            self._current = None
