"""Dhan ingress entry point — options market data feed.

Dual-mode operation:
  1. WS mode (default): subscribes to Dhan live feed for underlying LTP.
     Builds OHLCV bars and POSTs them to Flask /ingest on each bar close.
  2. Poll mode (--poll): polls Dhan REST for underlying LTP at fixed interval.
     Useful for testing without a live WS connection.

On each bar close the option chain is also fetched and stored for /options/decide.

Run:
  python3 -m dhan.main --symbol NIFTY --tf 5m --flask http://localhost:5000
  python3 -m dhan.main --symbol BANKNIFTY --tf 1m --poll --interval 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging

_setup_logging()
LOG = logging.getLogger("dhan.main")

from dhan.instruments import get as get_instrument
from dhan.option_chain import fetch_chain, get_underlying_ltp, nearest_expiry
from dhan.bar_builder import BarBuilder


def _post(flask_url: str, path: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urlreq.Request(
        flask_url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            LOG.info(f"[bar→flask] {path} {payload.get('bar_id', '')} → {body[:120]}")
    except Exception as e:
        LOG.warning(f"[bar→flask] POST {path} failed: {e}")


def _is_market_hours() -> bool:
    """NSE market: 9:15–15:30 IST = 3:45–10:00 UTC."""
    import datetime
    utc = datetime.datetime.now(datetime.timezone.utc)
    utc_minutes = utc.hour * 60 + utc.minute
    # 3:45 UTC = 225 min, 10:00 UTC = 600 min
    return 225 <= utc_minutes < 600


def _make_on_bar_close(symbol: str, tf: str, flask_url: str, instr, expiry_ref: list) -> callable:
    """Shared bar-close handler: post bar + refresh option chain."""
    def on_bar_close(payload: dict) -> None:
        _post(flask_url, "/ingest", payload)
        if _is_market_hours():
            try:
                chain = fetch_chain(instr, expiry_ref[0])
                ltp = payload["ohlc"]["c"]
                _post(flask_url, "/options/ingest", {
                    "symbol": symbol,
                    "underlying_ltp": ltp,
                    "expiry": expiry_ref[0],
                    "chain": chain,
                    "bar_id": payload.get("bar_id", ""),
                })
            except Exception as e:
                LOG.warning(f"[dhan.main] option chain fetch failed: {e}")
    return on_bar_close


def run_ws(
    symbol: str,
    tf: str,
    flask_url: str,
    price_step: float,
    dom: bool = False,
) -> None:
    """WS mode: live feed → bar builder → Flask.

    dom=True uses Full packet (DOM) → DomFootprintBuilder (real delta).
    dom=False uses Quote packet → BarBuilder (LTP-only, delta=0).
    """
    instr = get_instrument(symbol)
    expiry_ref = [nearest_expiry(instr)]
    on_bar_close = _make_on_bar_close(symbol, tf, flask_url, instr, expiry_ref)

    from dhan.ws_client import DhanWsClient

    if dom:
        from dhan.dom_builder import DomFootprintBuilder
        builder = DomFootprintBuilder(
            symbol=symbol, tf=tf, on_bar_close=on_bar_close, price_step=price_step
        )
        ws = DhanWsClient(instrument=instr, mode="full", on_snapshot=builder.on_snapshot)
        LOG.info(f"[dhan] WS DOM mode: {symbol} {tf} → {flask_url} (Full packet, real delta)")
    else:
        builder = BarBuilder(
            symbol=symbol, tf=tf, on_bar_close=on_bar_close, price_step=price_step
        )
        ws = DhanWsClient(instrument=instr, mode="quote", on_tick=builder.on_tick)
        LOG.info(f"[dhan] WS Quote mode: {symbol} {tf} → {flask_url} (LTP-only, delta=0)")

    ws.start()  # blocking


def run_poll(
    symbol: str,
    tf: str,
    flask_url: str,
    price_step: float,
    interval_s: float,
) -> None:
    """Poll mode: REST LTP polling → bar builder → Flask. No DOM (REST only)."""
    instr = get_instrument(symbol)
    expiry_ref = [nearest_expiry(instr)]
    on_bar_close = _make_on_bar_close(symbol, tf, flask_url, instr, expiry_ref)

    builder = BarBuilder(
        symbol=symbol, tf=tf, on_bar_close=on_bar_close, price_step=price_step
    )

    LOG.info(f"[dhan] poll mode: {symbol} {tf} every {interval_s}s → {flask_url}")
    import time as _time
    while True:
        if not _is_market_hours():
            LOG.debug("[dhan] outside market hours, sleeping 60s")
            _time.sleep(60)
            continue
        ltp = get_underlying_ltp(instr)
        if ltp is not None:
            builder.on_tick(int(_time.time() * 1000), ltp, 0.0)
        _time.sleep(interval_s)


def main() -> None:
    ap = argparse.ArgumentParser(description="Dhan options market data feed")
    ap.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK", "TCS"])
    ap.add_argument("--tf", default="5m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.05)
    ap.add_argument("--poll", action="store_true", help="Use REST polling instead of WS")
    ap.add_argument("--interval", type=float, default=15.0, help="Poll interval in seconds (--poll mode)")
    ap.add_argument("--dom", action="store_true",
                    help="Use Full packet (DOM) for real delta footprint instead of LTP-only Quote")
    args = ap.parse_args()

    if args.poll:
        run_poll(args.symbol, args.tf, args.flask, args.price_step, args.interval)
    else:
        run_ws(args.symbol, args.tf, args.flask, args.price_step, dom=args.dom)


if __name__ == "__main__":
    main()
