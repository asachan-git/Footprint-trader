"""Build footprint bars from Dhan Full packet (DOM) data.

Full packet provides per-update:
  LTP  — last traded price
  LTQ  — last traded quantity (size of the trade that just printed)
  depth[0..4] — 5-level order book: bid_price, bid_quantity, ask_price, ask_quantity

Delta computation (primary — LTQ + price direction):
  LTP rose  → buyer was aggressor (lifted ask) → ask_at_price[LTP] += LTQ
  LTP fell  → seller was aggressor (hit bid)   → bid_at_price[LTP] += LTQ
  LTP flat  → direction unknown; use book imbalance heuristic (see below)

Book imbalance (secondary — depth absorption):
  If ask_qty at a level dropped vs previous snapshot → buyers absorbed that ask
  If bid_qty at a level dropped vs previous snapshot → sellers absorbed that bid
  Used as supplemental signal; adds to ask/bid ladders at the affected price

This gives bybit-quality footprint (not perfectly identical but order-flow-comparable).

Output format: dhan_dom_v1 — same payload shape as bybit_v1 (bid_ladder/ask_ladder/delta)
so the existing pipeline (normalizer → state_store → Claude prompt) works unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
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
    bid_at_price: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    ask_at_price: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    first_price: float | None = None
    last_price: float | None = None
    high: float = float("-inf")
    low: float = float("inf")
    total_volume: float = 0.0
    trades: int = 0

    def add(self, price: float, qty: float, side: str) -> None:
        if self.first_price is None:
            self.first_price = price
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.total_volume += qty
        self.trades += 1
        if side == "Buy":
            self.ask_at_price[price] += qty   # buyer lifted ask
        else:
            self.bid_at_price[price] += qty   # seller hit bid

    def add_absorption(self, price: float, qty: float, side: str) -> None:
        """Add absorbed DOM volume (secondary signal, lower weight)."""
        if side == "Buy":
            self.ask_at_price[price] += qty * 0.5
        else:
            self.bid_at_price[price] += qty * 0.5

    def to_payload(self) -> dict:
        if self.first_price is None:
            return {}
        prices = set(self.bid_at_price) | set(self.ask_at_price)
        bid_ladder = sorted(
            [{"price": p, "vol": round(self.bid_at_price.get(p, 0.0), 2)} for p in prices],
            key=lambda x: x["price"],
        )
        ask_ladder = sorted(
            [{"price": p, "vol": round(self.ask_at_price.get(p, 0.0), 2)} for p in prices],
            key=lambda x: x["price"],
        )
        total_bid = sum(self.bid_at_price.values())
        total_ask = sum(self.ask_at_price.values())
        delta = round(total_ask - total_bid, 2)
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "close_ts": self.close_ts,
            "bar_id": _bar_id(self.symbol, self.tf, self.close_ts),
            "ohlc": {
                "o": self.first_price,
                "h": self.high,
                "l": self.low,
                "c": self.last_price,
            },
            "volume": round(self.total_volume, 2),
            "bid_ladder": bid_ladder,
            "ask_ladder": ask_ladder,
            "delta": delta,
            "trades": self.trades,
            "format": "dhan_dom_v1",
        }


class DomFootprintBuilder:
    """Accumulate Dhan Full packets into footprint bars.

    Call on_snapshot(full_packet) for each message received from MarketFeed.
    Calls on_bar_close(payload) when a bar closes.
    """

    def __init__(
        self,
        symbol: str,
        tf: str,
        on_bar_close: Callable[[dict], None],
        price_step: float = 0.05,
    ) -> None:
        if tf not in TF_SECONDS:
            raise ValueError(f"Unsupported tf {tf!r}")
        self._symbol = symbol
        self._tf = tf
        self._tf_s = TF_SECONDS[tf]
        self._on_bar_close = on_bar_close
        self._price_step = price_step
        self._current: _AccumBar | None = None

        # Previous snapshot state for absorption computation
        self._prev_ltp: float | None = None
        self._prev_depth: list[dict] | None = None  # 5-level depth from last packet

    def _round_price(self, p: float) -> float:
        return round(round(p / self._price_step) * self._price_step, 10)

    def _compute_absorption(
        self, curr_depth: list[dict], prev_depth: list[dict]
    ) -> list[tuple[float, float, str]]:
        """Return list of (price, absorbed_qty, side) from depth delta."""
        absorptions: list[tuple[float, float, str]] = []
        for curr, prev in zip(curr_depth, prev_depth):
            # Ask absorption: ask_qty decreased → buyers absorbed
            try:
                curr_ask_p = float(curr["ask_price"])
                curr_ask_q = float(curr["ask_quantity"])
                prev_ask_q = float(prev.get("ask_quantity", 0))
                absorbed_ask = prev_ask_q - curr_ask_q
                if absorbed_ask > 0 and curr_ask_p > 0:
                    absorptions.append((self._round_price(curr_ask_p), absorbed_ask, "Buy"))
            except (ValueError, KeyError):
                pass

            # Bid absorption: bid_qty decreased → sellers absorbed
            try:
                curr_bid_p = float(curr["bid_price"])
                curr_bid_q = float(curr["bid_quantity"])
                prev_bid_q = float(prev.get("bid_quantity", 0))
                absorbed_bid = prev_bid_q - curr_bid_q
                if absorbed_bid > 0 and curr_bid_p > 0:
                    absorptions.append((self._round_price(curr_bid_p), absorbed_bid, "Sell"))
            except (ValueError, KeyError):
                pass
        return absorptions

    def on_snapshot(self, full_packet: dict) -> None:
        """Process one Full packet from MarketFeed."""
        try:
            ltp = float(full_packet.get("LTP") or 0)
            ltq = float(full_packet.get("LTQ") or 0)
            volume = float(full_packet.get("volume") or 0)
            depth: list[dict] = full_packet.get("depth") or []
            ts_ms = int(time.time() * 1000)
        except (ValueError, TypeError):
            LOG.debug(f"[dom_builder] bad packet: {full_packet}")
            return

        if ltp <= 0:
            return

        ts_s = ts_ms // 1000
        bar_close_ts = _bar_close_ts(ts_s, self._tf_s)

        if self._current is None:
            self._current = _AccumBar(self._symbol, self._tf, bar_close_ts)

        # Bar boundary
        if bar_close_ts > self._current.close_ts:
            payload = self._current.to_payload()
            if payload:
                self._on_bar_close(payload)
                LOG.info(f"[dom_bar] {payload['bar_id']} o={payload['ohlc']['o']} h={payload['ohlc']['h']} l={payload['ohlc']['l']} c={payload['ohlc']['c']} delta={payload['delta']} trades={payload['trades']}")
            self._current = _AccumBar(self._symbol, self._tf, bar_close_ts)

        # Primary: LTQ + price direction
        if ltq > 0 and self._prev_ltp is not None:
            price = self._round_price(ltp)
            if ltp > self._prev_ltp:
                self._current.add(price, ltq, "Buy")
            elif ltp < self._prev_ltp:
                self._current.add(price, ltq, "Sell")
            else:
                # Price unchanged — use total book imbalance as tiebreak
                try:
                    total_buy = float(full_packet.get("total_buy_quantity") or 0)
                    total_sell = float(full_packet.get("total_sell_quantity") or 0)
                    side = "Buy" if total_buy >= total_sell else "Sell"
                    self._current.add(price, ltq, side)
                except Exception:
                    pass

        # Secondary: DOM absorption from depth delta
        if depth and self._prev_depth and len(depth) == len(self._prev_depth):
            for price, qty, side in self._compute_absorption(depth, self._prev_depth):
                self._current.add_absorption(price, qty, side)

        self._prev_ltp = ltp
        self._prev_depth = depth

    def flush(self) -> None:
        if self._current and self._current.trades > 0:
            payload = self._current.to_payload()
            if payload:
                self._on_bar_close(payload)
                LOG.info(f"[dom_bar] flush {payload['bar_id']} delta={payload['delta']}")
            self._current = None
