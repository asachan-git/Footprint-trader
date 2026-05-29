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
from .ws_client import stream_trades as ws_stream_trades
from .rest_poller import stream_trades as rest_stream_trades

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging; _setup_logging()
LOG = logging.getLogger("bybit.main")


def _post(flask_url: str, payload: dict, symbol_as: str | None = None) -> None:
    if symbol_as:
        payload["bar_id"] = payload["bar_id"].replace(payload["symbol"], symbol_as, 1)
        payload["symbol"] = symbol_as
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


async def run(symbol: str, tf: str, flask_url: str, price_step: float, category: str, use_rest: bool = False, symbol_as: str | None = None) -> None:
    builder = FootprintBuilder(
        symbol=symbol,
        tf=tf,
        on_bar_close=lambda payload: _post(flask_url, payload, symbol_as=symbol_as),
        price_step=price_step,
    )
    display = f"{symbol}→{symbol_as}" if symbol_as else symbol
    if use_rest:
        LOG.info(f"[bybit] REST polling {display} {tf} ({category}) → {flask_url}/ingest (price_step={price_step})")
        await rest_stream_trades(symbol, builder.on_tick, category=category)
    else:
        LOG.info(f"[bybit] streaming {display} {tf} ({category}) → {flask_url}/ingest (price_step={price_step})")
        await ws_stream_trades(symbol, builder.on_tick, category=category)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.1,
                    help="Round prices to this tick size for footprint cell grouping.")
    ap.add_argument("--category", default="linear", choices=["linear", "spot"],
                    help="linear=perpetuals (BTCUSDT), spot=spot pairs (PAXGUSDT)")
    ap.add_argument("--rest", action="store_true",
                    help="Use REST polling fallback instead of WebSocket (for geo-blocked regions)")
    ap.add_argument("--symbol-as", default=None, dest="symbol_as",
                    help="Remap symbol in bar payload (e.g. XAUUSDT→XAUTUSDT for ingest)")
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.tf, args.flask, args.price_step, args.category,
                    use_rest=args.rest, symbol_as=args.symbol_as))


if __name__ == "__main__":
    main()
