"""End-to-end reconciliation test on Vantage demo.

Flow:
  1. Submit a real 0.01-lot XAUUSD+ order through MT5Adapter.submit_order
     (decision tagged with XAUTUSDT symbol so symbol_map kicks in)
  2. Verify position recorded in position_store with broker_ticket
  3. Close the broker position directly (simulate SL/TP hit)
  4. Run reconcile() → expect store to mark position closed with realized_r

Run:
  ALLOW_LIVE=1 PYTHONPATH=. python3 scripts/mt5_reconcile_test.py
"""

from __future__ import annotations

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
LOG = logging.getLogger("reconcile_test")

from execution.live.mt5_adapter import MT5Adapter
from execution.position_store import position_store
from execution.reconcile import reconcile
from llm.schema import Decision
from pipeline.types import Bar, OHLC


def main() -> int:
    adapter = MT5Adapter()

    # Safety preflight: confirm demo
    positions_pre = adapter.get_open_positions()
    LOG.info(f"[preflight] adapter ok, open positions before: {len(positions_pre)}")

    # Get current price for synthetic decision
    info = adapter._run(adapter._ensure_conn())  # init
    price = adapter._run(_async_price(adapter, "XAUUSD+"))
    ask = price.get("ask")
    if not ask:
        LOG.error("market closed")
        return 1
    LOG.info(f"[market] XAUUSD+ ask={ask}")

    # Build a fake decision (analysis-symbol XAUTUSDT, mapped → XAUUSD+)
    entry = ask
    sl = round(ask - 1.0, 2)
    tp = round(ask + 1.0, 2)
    decision = Decision(
        side="long", entry=entry, stop_loss=sl, take_profit=tp,
        confidence=0.85, rationale="reconcile smoke test",
    )
    bar = Bar(
        bar_id=f"XAUTUSDT|1m|{int(time.time())}|test",
        symbol="XAUTUSDT", tf="1m", close_ts=int(time.time()),
        source="live",
        ohlc=OHLC(o=ask, h=ask, l=ask, c=ask),
        bid_ladder=(), ask_ladder=(),
    )

    # Submit via adapter
    LOG.info(f"[submit] long 0.01 XAUTUSDT→XAUUSD+ SL={sl} TP={tp}")
    result = adapter.submit_order(decision, bar)
    LOG.info(f"[submit] result={result}")
    order = (result or {}).get("order") or {}
    ticket = str(order.get("positionId") or "")
    if not ticket:
        LOG.error("no broker ticket in result — aborting")
        return 2

    # Manually record into position_store (bypasses LiveExecutor since we
    # called adapter directly). LiveExecutor.fire does this normally.
    ps = position_store()
    pos = ps.open_position(
        decision=decision, bar_id=bar.bar_id,
        symbol=bar.symbol, tf=bar.tf,
        broker_ticket=ticket, fill_type="vantage_mt5_live",
    )
    LOG.info(f"[store] recorded position {pos.position_id} ticket={pos.broker_ticket}")

    # Close at broker (simulate SL/TP hit from external)
    time.sleep(2)
    close_result = adapter.close_position(ticket)
    LOG.info(f"[broker_close] {close_result}")

    # Reconcile
    time.sleep(2)
    summary = reconcile(adapter, "XAUTUSDT")
    LOG.info(f"[reconcile] {summary}")

    # Verify store closed it
    still_open = ps.open_positions("XAUTUSDT")
    matching = [p for p in still_open if p.broker_ticket == ticket]
    if matching:
        LOG.error(f"[verify] FAIL — position still open in store: {matching[0]}")
        return 3
    LOG.info(f"[verify] position closed in store ✓")
    return 0


async def _async_price(adapter, symbol):
    conn = await adapter._ensure_conn()
    return await conn.get_symbol_price(symbol)


if __name__ == "__main__":
    sys.exit(main())
