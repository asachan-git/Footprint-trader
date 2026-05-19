"""Exness MT5 ingress entry point.

Subscribes via MetaApi to MT5 tick stream, accumulates ticks per bar,
POSTs each bar's exness_v1 payload to Flask /ingest.

Run:
  python3 -m exness.main --symbol XAUUSD --tf 1m --price-step 0.1

Env:
  METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION (default: new-york)
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

from .footprint_builder import FootprintBuilder
from .ws_client import stream_symbol

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging; _setup_logging()
# Silence metaapi SDK's chatty INFO logs
for _name in ("metaapi_cloud_sdk", "socketio", "engineio"):
    logging.getLogger(_name).setLevel(logging.WARNING)
LOG = logging.getLogger("exness.main")


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


async def run(symbol: str, tf: str, flask_url: str, price_step: float) -> None:
    builder = FootprintBuilder(
        symbol=symbol,
        tf=tf,
        on_bar_close=lambda payload: _post(flask_url, payload),
        price_step=price_step,
    )
    LOG.info(f"[exness] streaming {symbol} {tf} → {flask_url}/ingest (price_step={price_step})")
    await stream_symbol(symbol, builder.on_tick)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.1,
                    help="Round prices into footprint cells. XAUUSD: 0.1 = $0.10 cells.")
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.tf, args.flask, args.price_step))


if __name__ == "__main__":
    main()
