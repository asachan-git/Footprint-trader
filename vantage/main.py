"""Vantage MT5 ingress entry point — wraps exness/main with Vantage defaults.

Run:
  python3 -m vantage.main                              # XAUUSD, 0.1 step
  python3 -m vantage.main --symbol XAUUSD+             # ECN suffix variant
  python3 -m vantage.main --symbol GOLD --price-step 0.01

Env: METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logging_config import setup as _setup_logging
_setup_logging()
for _name in ("metaapi_cloud_sdk", "socketio", "engineio"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from exness.main import run as _run

LOG = logging.getLogger("vantage.main")


def main() -> None:
    ap = argparse.ArgumentParser(description="Vantage MT5 → Flask ingress (via MetaApi)")
    ap.add_argument("--symbol", default="XAUUSD",
                    help="Exact Vantage MT5 symbol. Run vantage.list_symbols to discover.")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.1,
                    help="Footprint cell size. XAU: 0.1pt = 1 tick.")
    args = ap.parse_args()
    LOG.info(f"[vantage] starting MT5 → Flask bridge for {args.symbol} ({args.tf}, step={args.price_step})")
    asyncio.run(_run(args.symbol, args.tf, args.flask, args.price_step))


if __name__ == "__main__":
    main()
