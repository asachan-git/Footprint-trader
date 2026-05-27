"""Bybit v5 public WebSocket — subscribes to publicTrade for a symbol.

Endpoint: wss://stream.bybit.com/v5/public/linear (USDT perpetuals)
Subscribe: {"op":"subscribe","args":["publicTrade.BTCUSDT"]}

Each trade message:
  {"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":<server_ms>,
   "data":[{"T":<trade_ts_ms>,"s":"BTCUSDT","S":"Buy"|"Sell","v":"<vol>","p":"<price>","i":"<id>","BT":false}]}

S = taker side: Buy → taker bought (lifted ask), Sell → taker sold (hit bid).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Callable

import websockets

LOG = logging.getLogger(__name__)

PUBLIC_LINEAR_URL = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_SPOT_URL   = "wss://stream.bybit.com/v5/public/spot"
CATEGORY_URLS = {"linear": PUBLIC_LINEAR_URL, "spot": PUBLIC_SPOT_URL}

_BACKOFF_BASE = 3.0     # first retry delay (s)
_BACKOFF_MAX = 300.0    # cap (s) — avoids hammering a throttled IP
_OPEN_TIMEOUT = 12.0    # handshake timeout (s)


async def stream_trades(
    symbol: str,
    on_trade: Callable[[int, float, float, str], None],
    category: str = "linear",
    ping_interval: float = 20.0,
) -> None:
    url = CATEGORY_URLS.get(category, PUBLIC_LINEAR_URL)
    """Connect, subscribe to publicTrade.<symbol>, dispatch each tick to on_trade."""
    sub = {"op": "subscribe", "args": [f"publicTrade.{symbol}"]}

    backoff = _BACKOFF_BASE
    while True:
        try:
            async with websockets.connect(url, ping_interval=ping_interval, open_timeout=_OPEN_TIMEOUT) as ws:
                await ws.send(json.dumps(sub))
                LOG.info(f"[bybit] subscribed publicTrade.{symbol} ({category})")
                backoff = _BACKOFF_BASE  # reset on successful connect
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("op") == "subscribe":
                        LOG.info(f"[bybit] subscribe ack: {msg}")
                        continue
                    if msg.get("topic", "").startswith("publicTrade."):
                        for t in msg.get("data", []):
                            try:
                                on_trade(int(t["T"]), float(t["p"]), float(t["v"]), t["S"])
                            except Exception as e:
                                LOG.warning(f"[bybit] tick parse failed: {e} {t}")
        except (websockets.ConnectionClosed, OSError) as e:
            # Exponential backoff with jitter — stop hammering a throttled IP
            delay = min(backoff, _BACKOFF_MAX) * (1.0 + random.random() * 0.3)
            LOG.warning(f"[bybit] connection lost: {e}; reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _BACKOFF_MAX)
