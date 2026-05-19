"""Capital.com WebSocket — subscribes to OHLC + market data quote stream.

Endpoint: wss://api-streaming-capital.backend-capital.com/connect

Auth: every outbound subscription message must include cst + securityToken.

Subscriptions:
  - marketData.subscribe       → bid/ask updates per epic (real-time quote stream)
  - OHLCMarketData.subscribe   → candle close events with OHLC

For tick-level footprint we use marketData (quote updates) and apply the
"tick rule": when bid/ask mid moves up vs prev mid → infer buyer agg; down → seller agg.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

from .auth import Session

LOG = logging.getLogger(__name__)

WS_URL = "wss://api-streaming-capital.backend-capital.com/connect"


async def stream_quotes(
    session: Session,
    epics: list[str],
    on_quote: Callable[[str, int, float, float], None],
    ping_interval: float = 30.0,
) -> None:
    """Subscribe to market data for given epics; dispatch (epic, ts_ms, bid, ask) per update."""
    sub_msg = {
        "destination": "marketData.subscribe",
        "correlationId": "1",
        "cst": session.cst,
        "securityToken": session.security_token,
        "payload": {"epics": epics},
    }

    backoff = 3.0
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=ping_interval) as ws:
                await ws.send(json.dumps(sub_msg))
                LOG.info(f"[capital] subscribed marketData {epics}")
                backoff = 3.0
                async for raw in ws:
                    msg = json.loads(raw)
                    dest = msg.get("destination", "")
                    if dest == "marketData.subscribe":
                        LOG.info(f"[capital] sub ack: {msg.get('status')}")
                        continue
                    if dest == "quote":
                        payload = msg.get("payload", {})
                        epic = payload.get("epic")
                        bid = payload.get("bid")
                        ask = payload.get("ofr")
                        ts_ms = payload.get("timestamp")
                        if epic and bid is not None and ask is not None and ts_ms is not None:
                            on_quote(epic, int(ts_ms), float(bid), float(ask))
        except (websockets.ConnectionClosed, OSError) as e:
            LOG.warning(f"[capital] connection lost: {e}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
