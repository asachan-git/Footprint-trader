"""Binance Futures ingress entry point.

Run:
  python3 -m binance.main --symbol BTCUSDT --tf 1m --price-step 1.0

No env vars needed — Binance futures market data is public.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from urllib import request as urlreq

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from bybit.footprint_builder import FootprintBuilder  # same builder, reuse
from .ws_client import stream_trades

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging; _setup_logging()
LOG = logging.getLogger("binance.main")


def _post(flask_url: str, payload: dict) -> None:
    payload["format"] = "binance_v1"
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
            LOG.info(f"[bar→flask] {payload['bar_id']} delta={payload['delta']:.2f} → {body[:200]}")
    except Exception as e:
        LOG.warning(f"[bar→flask] POST failed: {e}")


async def run(symbol: str, tf: str, flask_url: str, price_step: float) -> None:
    builder = FootprintBuilder(
        symbol=symbol,
        tf=tf,
        on_bar_close=lambda p: _post(flask_url, p),
        price_step=price_step,
    )
    LOG.info(f"[binance] streaming {symbol} {tf} → {flask_url}/ingest (price_step={price_step})")
    await stream_trades(symbol, builder.on_tick)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=1.0)
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.tf, args.flask, args.price_step))


if __name__ == "__main__":
    main()
