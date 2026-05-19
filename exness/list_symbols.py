"""Diagnose: list available symbols on the MetaApi-connected MT5 account.

Run: PYTHONPATH=. python3 -m exness.list_symbols
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("metaapi_cloud_sdk", "socketio", "engineio"):
    logging.getLogger(_n).setLevel(logging.WARNING)


async def main() -> None:
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

    syms = await conn.get_symbols()
    gold = [s for s in syms if "XAU" in s.upper() or "GOLD" in s.upper()]
    print(f"total symbols: {len(syms)}")
    print(f"gold-like: {gold}")
    print(f"first 30 symbols: {syms[:30]}")


if __name__ == "__main__":
    asyncio.run(main())
