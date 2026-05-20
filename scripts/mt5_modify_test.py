"""Smoke-test MT5 SL/TP modification on Vantage demo.

Flow:
  1. Open 0.01 lot XAUUSD+ long with initial SL/TP
  2. Modify SL upward (simulate break-even or trail)
  3. Verify new SL via get_open_positions
  4. Close position

Run:
  PYTHONPATH=. python3 scripts/mt5_modify_test.py
"""

from __future__ import annotations

import logging
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
LOG = logging.getLogger("modify_test")

from execution.live.mt5_adapter import MT5Adapter


def _async_price(adapter, symbol):
    async def go():
        conn = await adapter._ensure_conn()
        return await conn.get_symbol_price(symbol)
    return adapter._run(go())


def main() -> int:
    m = MT5Adapter()
    price = _async_price(m, "XAUUSD+")
    ask = price.get("ask")
    LOG.info(f"[market] ask={ask}")

    initial_sl = round(ask - 2.0, 2)
    tp = round(ask + 5.0, 2)
    LOG.info(f"[submit] long 0.01 XAUUSD+ SL={initial_sl} TP={tp}")

    # Direct MetaApi call (bypass risk-sizing — want 0.01 lot for this test)
    async def submit():
        conn = await m._ensure_conn()
        return await conn.create_market_buy_order(
            "XAUUSD+", 0.01,
            stop_loss=initial_sl, take_profit=tp,
            options={"comment": "FB|modify_test"},
        )
    result = m._run(submit())
    ticket = result.get("positionId")
    LOG.info(f"[fill] ticket={ticket}")

    time.sleep(2)

    # Move SL closer (simulate break-even: SL → ask - 0.5)
    new_sl = round(ask - 0.5, 2)
    LOG.info(f"[modify] SL → {new_sl} (TP unchanged at {tp})")
    mod = m.modify_position(ticket, stop_loss=new_sl)  # TP=None → adapter preserves
    LOG.info(f"[modify] result={mod}")

    time.sleep(2)
    positions = m.get_open_positions()
    matched = [p for p in positions if p.get("id") == ticket]
    if matched:
        LOG.info(f"[verify] post-modify SL={matched[0].get('stopLoss')} TP={matched[0].get('takeProfit')}")
        assert abs(matched[0].get("stopLoss") - new_sl) < 0.001, f"SL not updated: {matched[0].get('stopLoss')} vs {new_sl}"
        LOG.info("[verify] SL updated ✓")

    # Close
    close = m.close_position(ticket)
    LOG.info(f"[close] {close}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
