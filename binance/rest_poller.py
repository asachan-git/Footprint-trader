"""Binance aggTrade REST polling — fallback when WS is geo-blocked.

Polls aggTrades every POLL_INTERVAL_S seconds.
Deduplicates by aggTradeId (`a`). Same on_trade callback signature as ws_client.

Endpoints:
  spot    — https://api.binance.com/api/v3/aggTrades
  futures — https://fapi.binance.com/fapi/v1/aggTrades
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Callable
from urllib import request as urlreq

LOG = logging.getLogger(__name__)

_BASE_URLS = {
    "spot":    "https://api.binance.com/api/v3/aggTrades",
    "futures": "https://fapi.binance.com/fapi/v1/aggTrades",
}
POLL_INTERVAL_S = 5.0
_BACKOFF_MAX = 120.0

# Allowlist applies to spot only (XAUT). Futures venue uses XAUUSDT / BTCUSDT
# which are not in this set — skipped via the venue branch below.
ALLOWED_SYMBOLS_SPOT = {"XAUTUSDT", "XAUTUSDC", "BTCUSDT", "XAUUSDT"}


async def stream_trades(
    symbol: str,
    on_trade: Callable[[int, float, float, str], None],
    limit: int = 1000,
    venue: str = "futures",
) -> None:
    """Poll aggTrades for symbol; dispatch (ts_ms, price, qty, side) per new trade."""
    base = _BASE_URLS.get(venue)
    if base is None:
        raise ValueError(f"[binance-rest] unknown venue {venue!r}, expected one of {list(_BASE_URLS)}")
    if venue == "spot" and symbol not in ALLOWED_SYMBOLS_SPOT:
        raise ValueError(f"[binance-rest] spot symbol {symbol!r} not in allowlist {ALLOWED_SYMBOLS_SPOT}")

    _seen_deque: deque[int] = deque(maxlen=50_000)
    seen: set[int] = set()
    backoff = POLL_INTERVAL_S

    LOG.info(f"[binance-rest:{venue}] polling {symbol} every {POLL_INTERVAL_S}s")

    while True:
        try:
            url = f"{base}?symbol={symbol}&limit={limit}"
            req = urlreq.Request(url, headers={"User-Agent": "FootprintBiot/1.0"})
            with urlreq.urlopen(req, timeout=10) as resp:
                trades = json.loads(resp.read())

            new_trades = [t for t in trades if t["a"] not in seen]

            # Process chronologically (API returns oldest→newest)
            for t in new_trades:
                aid = t["a"]
                if len(_seen_deque) == _seen_deque.maxlen:
                    evicted = _seen_deque[0]
                    seen.discard(evicted)
                _seen_deque.append(aid)
                seen.add(aid)
                try:
                    side = "Sell" if t["m"] else "Buy"  # m=isBuyerMaker: true→seller is taker
                    on_trade(int(t["T"]), float(t["p"]), float(t["q"]), side)
                except Exception as e:
                    LOG.warning(f"[binance-rest] tick parse failed: {e} {t}")

            backoff = POLL_INTERVAL_S
            await asyncio.sleep(POLL_INTERVAL_S)

        except Exception as e:
            LOG.warning(f"[binance-rest] poll failed: {e}; retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
