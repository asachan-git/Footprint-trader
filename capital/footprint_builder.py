"""Bid/ask quote stream → pseudo-footprint via tick rule.

Capital.com is a CFD broker — no centralized trade tape with aggressor side.
Each quote update gives (bid, ask). We infer:
  - mid_t = (bid + ask) / 2
  - If mid_t > mid_{t-1} → buyer aggression (uptick); attribute size to ask side
  - If mid_t < mid_{t-1} → seller aggression (downtick)
  - Tie / no prev: skip

Size is set to 1.0 per tick (no trade volume reported). For relative analysis
this still produces meaningful delta + imbalance over a bar window.
"""

from __future__ import annotations

import hashlib
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
    first_mid: float | None = None
    last_mid: float | None = None
    high: float = float("-inf")
    low: float = float("inf")
    upticks: int = 0
    downticks: int = 0

    def add(self, mid: float, side: str, tick_size: float = 1.0) -> None:
        if self.first_mid is None:
            self.first_mid = mid
        self.last_mid = mid
        self.high = max(self.high, mid)
        self.low = min(self.low, mid)
        if side == "Buy":
            self.ask_at_price[mid] += tick_size
            self.upticks += 1
        else:
            self.bid_at_price[mid] += tick_size
            self.downticks += 1

    def to_payload(self) -> dict:
        prices = set(self.bid_at_price) | set(self.ask_at_price)
        bid_ladder = [{"price": p, "vol": self.bid_at_price.get(p, 0.0)} for p in sorted(prices)]
        ask_ladder = [{"price": p, "vol": self.ask_at_price.get(p, 0.0)} for p in sorted(prices)]
        total_bid = sum(self.bid_at_price.values())
        total_ask = sum(self.ask_at_price.values())
        poc = max(prices, key=lambda p: self.bid_at_price.get(p, 0) + self.ask_at_price.get(p, 0)) if prices else None
        return {
            "format": "capital_v1",
            "source": "live",
            "bar_id": _bar_id(self.symbol, self.tf, self.close_ts),
            "symbol": self.symbol,
            "tf": self.tf,
            "close_ts": self.close_ts,
            "ohlc": {
                "o": self.first_mid or 0.0,
                "h": self.high if self.high != float("-inf") else 0.0,
                "l": self.low if self.low != float("inf") else 0.0,
                "c": self.last_mid or 0.0,
            },
            "bid_ladder": bid_ladder,
            "ask_ladder": ask_ladder,
            "delta": total_ask - total_bid,
            "buyvolume": total_ask,
            "sellvolume": total_bid,
            "poc": poc,
            "trades": self.upticks + self.downticks,
        }


class FootprintBuilder:
    def __init__(
        self,
        symbol: str,
        tf: str,
        on_bar_close: Callable[[dict], None],
        price_step: float = 0.1,
    ):
        self.symbol = symbol
        self.tf = tf
        self.tf_seconds = TF_SECONDS[tf]
        self.on_bar_close = on_bar_close
        self.price_step = price_step
        self._current: _AccumBar | None = None
        self._prev_mid: float | None = None

    def _bucket_price(self, p: float) -> float:
        if self.price_step <= 0:
            return p
        return round(p / self.price_step) * self.price_step

    def on_quote(self, ts_ms: int, bid: float, ask: float) -> None:
        mid = (bid + ask) / 2

        # Tick rule for aggressor inference
        if self._prev_mid is None:
            self._prev_mid = mid
            return
        if mid > self._prev_mid:
            side = "Buy"
        elif mid < self._prev_mid:
            side = "Sell"
        else:
            return  # no change → no info
        self._prev_mid = mid

        ts_s = ts_ms // 1000
        close_ts = _bar_close_ts(ts_s, self.tf_seconds)

        if self._current is None:
            self._current = _AccumBar(self.symbol, self.tf, close_ts)
        elif close_ts != self._current.close_ts:
            self.on_bar_close(self._current.to_payload())
            self._current = _AccumBar(self.symbol, self.tf, close_ts)

        self._current.add(self._bucket_price(mid), side)
