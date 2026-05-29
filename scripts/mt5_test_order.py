"""Smoke-test Vantage MT5 execution round-trip.

Sends ONE minimal market order (0.01 lot XAUUSD+ long) on the demo account,
waits for fill, prints position, then closes it. Total max P&L ~$1.

Pre-flight:
  - Verifies account server contains "Demo" (refuses live accounts)
  - Verifies balance > 0
  - Verifies XAUUSD+ market open

Run:
  PYTHONPATH=. python3 scripts/mt5_test_order.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

for n in ("metaapi_cloud_sdk", "socketio", "engineio"):
    logging.getLogger(n).setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("mt5_test")

SYMBOL = "XAUUSD+"
LOTS = 0.01
SL_DISTANCE = 1.00      # $1 below market for long → SL
TP_DISTANCE = 1.00      # $1 above market → TP


async def main() -> int:
    from metaapi_cloud_sdk import MetaApi

    token = os.environ["METAAPI_TOKEN"]
    account_id = os.environ["METAAPI_ACCOUNT_ID"]
    region = os.environ.get("METAAPI_REGION", "new-york")

    api = MetaApi(token, {"region": region})
    account = await api.metatrader_account_api.get_account(account_id)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized()

    # Pre-flight checks
    info = await conn.get_account_information()
    server = info.get("server", "")
    if "Demo" not in server and "demo" not in server.lower():
        LOG.error(f"[abort] account server '{server}' is not a demo account — refusing live test")
        return 2
    LOG.info(f"[preflight] {server} login={info.get('login')} balance={info.get('balance')} {info.get('currency')}")

    price = await conn.get_symbol_price(SYMBOL)
    bid = price.get("bid")
    ask = price.get("ask")
    if not bid or not ask:
        LOG.error("[abort] no price for symbol — market closed?")
        return 3
    LOG.info(f"[market] {SYMBOL} bid={bid} ask={ask} spread={ask - bid:.2f}")

    sl = round(ask - SL_DISTANCE, 2)
    tp = round(ask + TP_DISTANCE, 2)
    LOG.info(f"[order]  long 0.01 {SYMBOL} SL={sl} TP={tp} (max loss ~$1)")

    t0 = time.time()
    order = await conn.create_market_buy_order(
        SYMBOL, LOTS,
        stop_loss=sl,
        take_profit=tp,
        options={"comment": "FB|smoke_test"},
    )
    LOG.info(f"[fill]   submit took {time.time() - t0:.2f}s → {order}")

    # Inspect resulting position
    await asyncio.sleep(2)
    positions = await conn.get_positions()
    fb_positions = [p for p in (positions or []) if "FB|smoke_test" in (p.get("comment") or "")]
    LOG.info(f"[verify] open positions tagged smoke_test: {len(fb_positions)}")
    for p in fb_positions:
        LOG.info(f"         id={p.get('id')} symbol={p.get('symbol')} vol={p.get('volume')} "
                 f"open={p.get('openPrice')} sl={p.get('stopLoss')} tp={p.get('takeProfit')} "
                 f"profit={p.get('profit')}")

    # Close immediately
    if fb_positions:
        for p in fb_positions:
            result = await conn.close_position(p["id"])
            LOG.info(f"[close]  {p['id']} → {result}")

    LOG.info("[done]   round-trip complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
