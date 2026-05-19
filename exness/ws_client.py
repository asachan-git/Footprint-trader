"""MetaApi.cloud streaming client.

Connects to a provisioned MT5 account (Exness) via metaapi-cloud-sdk and
streams symbol prices/ticks. Each tick fires the supplied callback.

Setup:
  1. Sign up at metaapi.cloud (free tier)
  2. Provision MT5 account (Exness server + login + investor password)
  3. Generate auth token in Profile → Tokens
  4. Wait for account status = CONNECTED

Env vars expected:
  METAAPI_TOKEN     — auth token from MetaApi profile
  METAAPI_ACCOUNT_ID — UUID of the provisioned MT5 account
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

LOG = logging.getLogger(__name__)


async def stream_symbol(
    symbol: str,
    on_tick: Callable[[int, float | None, float | None, float | None, float, int], None],
) -> None:
    """Subscribe to MT5 tick + quote stream for `symbol` and dispatch to on_tick.

    Callback signature: (ts_ms, bid, ask, last, volume, flags)
    """
    from metaapi_cloud_sdk import MetaApi, SynchronizationListener   # lazy import — optional dep

    token = os.environ["METAAPI_TOKEN"]
    account_id = os.environ["METAAPI_ACCOUNT_ID"]
    region = os.environ.get("METAAPI_REGION", "new-york")

    api = MetaApi(token, {"region": region})
    account = await api.metatrader_account_api.get_account(account_id)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()

    connection = account.get_streaming_connection()
    await connection.connect()
    await connection.wait_synchronized()

    tick_count = [0]

    class TickListener(SynchronizationListener):
        async def on_symbol_price_updated(self, instance_index, price):
            if price.get("symbol") != symbol:
                return
            tick_count[0] += 1
            import time as _time
            t = price.get("time")
            if isinstance(t, (int, float)):
                ts_ms = int(t)
            elif isinstance(t, str):
                try:
                    from datetime import datetime
                    ts_ms = int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    ts_ms = int(_time.time() * 1000)
            else:
                ts_ms = int(_time.time() * 1000)
            bid = price.get("bid")
            ask = price.get("ask")
            last = price.get("last")
            vol = float(price.get("volume", 0) or 0)
            flags = int(price.get("flags", 0) or 0)
            if tick_count[0] <= 5 or tick_count[0] % 50 == 0:
                LOG.info(f"[exness tick #{tick_count[0]}] {symbol} bid={bid} ask={ask} last={last} flags={flags}")
            on_tick(ts_ms, bid, ask, last, vol, flags)

    listener = TickListener()
    connection.add_synchronization_listener(listener)

    async def subscribe_with_retry() -> None:
        for attempt in range(5):
            try:
                await connection.subscribe_to_market_data(symbol)
                LOG.info(f"[exness] subscribed market data {symbol}")
                return
            except Exception as e:
                LOG.warning(f"[exness] subscribe attempt {attempt+1} failed: {e}")
                await asyncio.sleep(3)
        LOG.error(f"[exness] could not subscribe to {symbol} after 5 attempts")

    await subscribe_with_retry()

    # Keepalive: re-subscribe every 5 min + detect stale tick stream
    last_tick_count = [0]
    while True:
        await asyncio.sleep(300)
        if tick_count[0] == last_tick_count[0]:
            LOG.warning(f"[exness] no ticks in 5 min (total={tick_count[0]}), re-subscribing...")
            await subscribe_with_retry()
        else:
            LOG.info(f"[exness] keepalive ok — {tick_count[0]} ticks so far")
        last_tick_count[0] = tick_count[0]
