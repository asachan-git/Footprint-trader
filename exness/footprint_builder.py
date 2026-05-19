"""Accumulate MT5 ticks into per-bar footprint.

MT5 tick has `flags` indicating aggressor:
  TICK_FLAG_BUY  = 4   → buyer-initiated (taker bought, lifted ask)
  TICK_FLAG_SELL = 8   → seller-initiated (taker sold, hit bid)

For XAUUSD spot via Exness, tick.last may not always be populated. Fallback:
  - If `last` present and `flags` set → trust the flag
  - Else use Lee-Ready inference on (price vs bid/ask)
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}

# MT5 tick flags
TICK_FLAG_BID = 2
TICK_FLAG_ASK = 1
TICK_FLAG_LAST = 16
TICK_FLAG_VOLUME = 32
TICK_FLAG_BUY = 4
TICK_FLAG_SELL = 8


def _bar_close_ts(tick_ts_s: int, tf_seconds: int) -> int:
    return ((tick_ts_s // tf_seconds) + 1) * tf_seconds


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    h = hashlib.sha1(f"{symbol}|{tf}|{close_ts}".encode()).hexdigest()[:16]
    return f"{symbol}|{tf}|{close_ts}|{h}"


def infer_side(price: float, bid: float | None, ask: float | None, flags: int) -> str | None:
    """Returns 'Buy' (taker bought) | 'Sell' (taker sold) | None."""
    if flags & TICK_FLAG_BUY:
        return "Buy"
    if flags & TICK_FLAG_SELL:
        return "Sell"
    # Lee-Ready fallback
    if ask is not None and price >= ask:
        return "Buy"
    if bid is not None and price <= bid:
        return "Sell"
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2
        return "Buy" if price > mid else "Sell" if price < mid else None
    return None


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

    def add(self, price: float, size: float, side: str) -> None:
        if self.first_price is None:
            self.first_price = price
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.trades += 1
        if side == "Buy":
            self.ask_at_price[price] += size
        else:
            self.bid_at_price[price] += size

    def to_payload(self) -> dict:
        prices = set(self.bid_at_price) | set(self.ask_at_price)
        bid_ladder = [{"price": p, "vol": self.bid_at_price.get(p, 0.0)} for p in sorted(prices)]
        ask_ladder = [{"price": p, "vol": self.ask_at_price.get(p, 0.0)} for p in sorted(prices)]
        total_bid = sum(self.bid_at_price.values())
        total_ask = sum(self.ask_at_price.values())
        poc = max(prices, key=lambda p: self.bid_at_price.get(p, 0) + self.ask_at_price.get(p, 0)) if prices else None
        return {
            "format": "exness_v1",
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
            "delta": total_ask - total_bid,
            "buyvolume": total_ask,
            "sellvolume": total_bid,
            "poc": poc,
            "trades": self.trades,
        }


class FootprintBuilder:
    def __init__(
        self,
        symbol: str,
        tf: str,
        on_bar_close: Callable[[dict], None],
        price_step: float = 0.0,
        default_size: float = 1.0,
    ):
        self.symbol = symbol
        self.tf = tf
        self.tf_seconds = TF_SECONDS[tf]
        self.on_bar_close = on_bar_close
        self.price_step = price_step
        self.default_size = default_size
        self._current: _AccumBar | None = None
        self._last_bid: float | None = None
        self._last_ask: float | None = None

    def _bucket_price(self, p: float) -> float:
        if self.price_step <= 0:
            return p
        return round(p / self.price_step) * self.price_step

    def on_tick(self, ts_ms: int, bid: float | None, ask: float | None,
                last: float | None, volume: float, flags: int) -> None:
        # Track latest quote
        prev_mid = (self._last_bid + self._last_ask) / 2 if (self._last_bid and self._last_ask) else None
        if bid is not None:
            self._last_bid = bid
        if ask is not None:
            self._last_ask = ask

        if last is not None:
            # Trade tick path — use flags or Lee-Ready against bid/ask
            price = last
            side = infer_side(price, self._last_bid, self._last_ask, flags)
            size = volume if volume > 0 else self.default_size
        else:
            # Quote-only tick (Forex CFD typical) — use tick rule on mid movement
            if self._last_bid is None or self._last_ask is None or prev_mid is None:
                return
            mid = (self._last_bid + self._last_ask) / 2
            if mid > prev_mid:
                side = "Buy"
            elif mid < prev_mid:
                side = "Sell"
            else:
                return  # no info
            price = mid
            size = self.default_size

        if side is None:
            return

        ts_s = ts_ms // 1000
        close_ts = _bar_close_ts(ts_s, self.tf_seconds)

        if self._current is None:
            self._current = _AccumBar(self.symbol, self.tf, close_ts)
        elif close_ts != self._current.close_ts:
            self.on_bar_close(self._current.to_payload())
            self._current = _AccumBar(self.symbol, self.tf, close_ts)

        self._current.add(self._bucket_price(price), size, side)
