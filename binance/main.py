"""Binance ingress entry point.

Run:
  python3 -m binance.main --symbol BTCUSDT --tf 1m --price-step 1.0 --venue futures

  # Remap XAUUSDT → XAUTUSDT so Binance Futures gold flows into the same
  # state-store key the system was using:
  python3 -m binance.main --symbol XAUUSDT --symbol-as XAUTUSDT --tf 1m --price-step 0.1 --venue futures

`--venue` selects spot (stream.binance.com / api.binance.com) or futures
(fstream.binance.com / fapi.binance.com). Defaults to futures.

No env vars needed — Binance market data streams are public.
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

from bybit.footprint_builder import FootprintBuilder, _bar_id  # same builder, reuse
from .ws_client import stream_trades
from .rest_poller import stream_trades as rest_stream_trades

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging; _setup_logging()
LOG = logging.getLogger("binance.main")


def _post(flask_url: str, payload: dict, symbol_as: str | None = None) -> None:
    payload["format"] = "binance_v1"
    if symbol_as:
        # Remap symbol so downstream state_store key matches target symbol.
        # Recompute bar_id from the target symbol — a string-replace would leave
        # the hash keyed to the source symbol, breaking bar_id idempotency
        # against other producers (fetch_history, rebuild_footprint_history)
        # that hash the target symbol.
        payload["symbol"] = symbol_as
        payload["bar_id"] = _bar_id(symbol_as, payload["tf"], payload["close_ts"])
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


async def run(symbol: str, tf: str, flask_url: str, price_step: float,
              symbol_as: str | None = None, use_rest: bool = False,
              venue: str = "futures") -> None:
    builder = FootprintBuilder(
        symbol=symbol,
        tf=tf,
        on_bar_close=lambda p: _post(flask_url, p, symbol_as=symbol_as),
        price_step=price_step,
    )
    display = f"{symbol}→{symbol_as}" if symbol_as else symbol
    mode = "REST polling" if use_rest else "streaming"
    LOG.info(f"[binance:{venue}] {mode} {display} {tf} → {flask_url}/ingest (price_step={price_step})")
    if use_rest:
        await rest_stream_trades(symbol, builder.on_tick, venue=venue)
    else:
        await stream_trades(symbol, builder.on_tick, venue=venue)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--symbol-as", default=None,
                    help="Remap symbol in payload before posting (e.g. XAUUSDT→XAUTUSDT)")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=1.0)
    ap.add_argument("--rest", action="store_true",
                    help="Use REST polling instead of WebSocket (for geo-blocked regions)")
    ap.add_argument("--venue", default="futures", choices=["spot", "futures"],
                    help="Binance venue. Default 'futures' (fapi/fstream).")
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.tf, args.flask, args.price_step,
                    symbol_as=args.symbol_as, use_rest=args.rest, venue=args.venue))


if __name__ == "__main__":
    main()
