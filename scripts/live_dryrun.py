"""Supervised LIVE dry-run of the full execution dispatch path.

Exercises the production chain that the direct-adapter smoke tests skipped:
  dispatch(decision, bar, settings[mode=live])
    → router._live() [ALLOW_LIVE gate]
    → LiveExecutor.fire
    → MT5Adapter.submit_order  (news/spread/tradable gates, risk lots, qty_pct)
    → position_store.open_position (broker_ticket)
    → cycle_store.open_cycle

A synthetic high-confidence Decision is injected (not Claude) so the order
is guaranteed to fire — this tests the WIRING, not the model. Tight SL/TP,
small lot. After verifying the position + cycle were recorded, the broker
position is closed and the dry-run records cleaned from the JSONL logs.

Requires: ALLOW_LIVE=1, demo account, XAU market open.

Run:
  ALLOW_LIVE=1 PYTHONPATH=. python3 scripts/live_dryrun.py
"""

from __future__ import annotations

import json
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
LOG = logging.getLogger("live_dryrun")

import yaml

from execution.router import dispatch
from execution.live.mt5_adapter import MT5Adapter
from execution.position_store import position_store, POSITIONS_LOG
from execution.cycle_store import cycle_store, CYCLES_LOG
from llm.schema import Decision
from pipeline.types import Bar, OHLC


def main() -> int:
    if os.environ.get("ALLOW_LIVE") != "1":
        LOG.error("ALLOW_LIVE != 1 — refusing. Re-run with ALLOW_LIVE=1")
        return 2

    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    settings = dict(settings)
    settings["mode"] = "live"   # force live for this dry-run only (in-memory)

    adapter = MT5Adapter()
    price = adapter._run(_async_price(adapter, "XAUUSD+"))
    ask = price.get("ask")
    bid = price.get("bid")
    if not ask:
        LOG.error("no XAU quote — market closed?")
        return 3
    LOG.info(f"[market] XAUUSD+ bid={bid} ask={ask}")

    # Synthetic decision (analysis symbol XAUTUSDT → mapped to XAUUSD+)
    entry = ask
    sl = round(ask - 2.0, 2)
    tp = round(ask + 4.0, 2)
    decision = Decision(
        side="long", entry=entry, stop_loss=sl, take_profit=tp,
        confidence=0.90, qty_pct=0.5,
        rationale="LIVE DRY-RUN synthetic decision (execution wiring test)",
    )
    bar = Bar(
        bar_id=f"XAUTUSDT|1m|{int(time.time())}|dryrun",
        symbol="XAUTUSDT", tf="1m", close_ts=int(time.time()),
        source="live", ohlc=OHLC(o=ask, h=ask, l=ask, c=ask),
        bid_ladder=(), ask_ladder=(),
    )

    LOG.info(f"[dispatch] mode=live long XAUTUSDT→XAUUSD+ entry={entry} SL={sl} TP={tp} qty_pct=0.5")
    result = dispatch(decision, bar, settings)
    LOG.info(f"[dispatch] result keys: {list(result.keys())}")
    LOG.info(f"[dispatch] lots={result.get('lots')} sizing={result.get('sizing')}")

    order = result.get("order") or {}
    ticket = str(order.get("positionId") or "")
    pid = result.get("position_id") or ""
    LOG.info(f"[result] broker_ticket={ticket} position_id={pid}")

    if not ticket:
        LOG.error(f"[FAIL] no broker ticket — result={result}")
        return 4

    # Verify store + cycle
    time.sleep(1)
    ps = position_store()
    pos = next((p for p in ps.open_positions("XAUTUSDT") if p.broker_ticket == ticket), None)
    cyc = cycle_store().by_position_id(pid) if pid else None
    LOG.info(f"[verify] position in store: {'YES' if pos else 'NO'}  cycle: {'YES' if cyc else 'NO'}")
    if pos:
        LOG.info(f"         pos {pos.position_id} side={pos.side} entry={pos.avg_entry} sl={pos.stop_loss} ticket={pos.broker_ticket}")
    if cyc:
        LOG.info(f"         cycle {cyc.cycle_id} dir={cyc.direction} status={cyc.status}")

    # Cleanup: close broker + mark store closed
    LOG.info(f"[cleanup] closing broker ticket {ticket}")
    adapter.close_position(ticket)
    if pid:
        ps.close_position(pid, "dry-run cleanup", 0.0)
        c = cycle_store().by_position_id(pid)
        if c:
            cycle_store().close_cycle(c.cycle_id, 0.0, "dry-run cleanup")

    LOG.info("[done] dry-run complete — verify the chain above, records closed")
    LOG.info("NOTE: positions.jsonl / cycles.jsonl now contain dry-run rows (closed). "
             "Remove manually if you want them gone.")
    return 0


async def _async_price(adapter, symbol):
    conn = await adapter._ensure_conn()
    return await conn.get_symbol_price(symbol)


if __name__ == "__main__":
    sys.exit(main())
