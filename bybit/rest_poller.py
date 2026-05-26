"""Bybit REST polling trade feed — fallback when WS is geo-blocked.

Polls /v5/market/recent-trade every POLL_INTERVAL_S seconds.
Deduplicates by execId. Same on_trade callback signature as ws_client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Callable
from urllib import request as urlreq

LOG = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com/v5/market/recent-trade"
POLL_INTERVAL_S = 5.0
_BACKOFF_MAX = 120.0


async def stream_trades(
    symbol: str,
    on_trade: Callable[[int, float, float, str], None],
    category: str = "linear",
    limit: int = 1000,
) -> None:
    """Poll recent trades for symbol; dispatch (ts_ms, price, qty, side) per new trade."""
    # Bounded deque evicts oldest execIds automatically — prevents re-dispatch after backoff
    _seen_deque: deque[str] = deque(maxlen=50_000)
    seen: set[str] = set()
    backoff = POLL_INTERVAL_S

    LOG.info(f"[bybit-rest] polling {symbol} ({category}) every {POLL_INTERVAL_S}s")

    while True:
        try:
            url = f"{BASE_URL}?symbol={symbol}&category={category}&limit={limit}"
            req = urlreq.Request(url, headers={"User-Agent": "FootprintBiot/1.0"})
            with urlreq.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            if data.get("retCode") != 0:
                LOG.warning(f"[bybit-rest] API error: {data.get('retMsg')}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            trades = data.get("result", {}).get("list", [])
            new_trades = [t for t in trades if t["execId"] not in seen]

            # Process in chronological order (API returns newest first)
            for t in reversed(new_trades):
                eid = t["execId"]
                # When deque is full, oldest entry is auto-evicted — remove from set too
                if len(_seen_deque) == _seen_deque.maxlen:
                    evicted = _seen_deque[0]
                    seen.discard(evicted)
                _seen_deque.append(eid)
                seen.add(eid)
                try:
                    side = "Buy" if t["side"] == "Buy" else "Sell"
                    on_trade(int(t["time"]), float(t["price"]), float(t["size"]), side)
                except Exception as e:
                    LOG.warning(f"[bybit-rest] tick parse failed: {e} {t}")

            backoff = POLL_INTERVAL_S
            await asyncio.sleep(POLL_INTERVAL_S)

        except Exception as e:
            LOG.warning(f"[bybit-rest] poll failed: {e}; retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
