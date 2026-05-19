"""Bybit ingress entry point.

Subscribes to public trade stream, accumulates ticks into bar-close footprint,
POSTs each closed bar's bybit_v1 payload to Flask /ingest.

Run:
  python3 -m bybit.main --symbol BTCUSDT --tf 1m --price-step 0.1 --flask http://localhost:5000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from urllib import request as urlreq

from .footprint_builder import FootprintBuilder
from .ws_client import stream_trades

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging; _setup_logging()
LOG = logging.getLogger("bybit.main")


def _post(flask_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urlreq.Request(
        flask_url.rstrip("/") + "/ingest",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            LOG.info(f"[bar→flask] {payload['bar_id']} delta={payload['delta']} → {body[:200]}")
    except Exception as e:
        LOG.warning(f"[bar→flask] POST failed: {e}")


async def run(symbol: str, tf: str, flask_url: str, price_step: float, category: str) -> None:
    builder = FootprintBuilder(
        symbol=symbol,
        tf=tf,
        on_bar_close=lambda payload: _post(flask_url, payload),
        price_step=price_step,
    )
    LOG.info(f"[bybit] streaming {symbol} {tf} ({category}) → {flask_url}/ingest (price_step={price_step})")
    await stream_trades(symbol, builder.on_tick, category=category)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.1,
                    help="Round prices to this tick size for footprint cell grouping.")
    ap.add_argument("--category", default="linear", choices=["linear", "spot"],
                    help="linear=perpetuals (BTCUSDT), spot=spot pairs (PAXGUSDT)")
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.tf, args.flask, args.price_step, args.category))


if __name__ == "__main__":
    main()
