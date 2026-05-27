"""Binance Futures aggTrade WebSocket stream.

Endpoint: wss://stream.binance.com:9443/ws/<symbol_lower>@aggTrade
No auth needed. Free public stream.

aggTrade fields used:
  T  — trade time ms
  p  — price
  q  — quantity
  m  — isBuyerMaker: false = taker bought (Buy); true = taker sold (Sell)

No signup, no API key, no rate limits for market data streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

LOG = logging.getLogger(__name__)

BASE = "wss://stream.binance.com:9443/ws"


async def stream_trades(
    symbol: str,
    on_trade: Callable[[int, float, float, str], None],
    ping_interval: float = 20.0,
) -> None:
    """Stream aggTrade for `symbol`; dispatch (ts_ms, price, qty, side) per trade."""
    url = f"{BASE}/{symbol.lower()}@aggTrade"
    backoff = 3.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=ping_interval) as ws:
                LOG.info(f"[binance] connected {url}")
                backoff = 3.0
                msg_count = 0
                async for raw in ws:
                    msg = json.loads(raw)
                    msg_count += 1
                    if msg_count <= 3:
                        LOG.info(f"[binance] msg #{msg_count}: {str(msg)[:200]}")
                    if msg.get("e") != "aggTrade":
                        continue
                    ts_ms = int(msg["T"])
                    price = float(msg["p"])
                    qty = float(msg["q"])
                    side = "Sell" if msg["m"] else "Buy"
                    on_trade(ts_ms, price, qty, side)
        except (websockets.ConnectionClosed, OSError) as e:
            LOG.warning(f"[binance] disconnected: {e}; reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
