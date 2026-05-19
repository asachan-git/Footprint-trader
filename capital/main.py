"""Capital.com ingress entry point.

Run:
  python3 -m capital.main --epic GOLD --tf 1m --price-step 0.1

Env: CAPITAL_API_KEY, CAPITAL_IDENTIFIER (email), CAPITAL_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from urllib import request as urlreq

from .auth import login
from .footprint_builder import FootprintBuilder
from .ws_client import stream_quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("capital.main")


def _post(flask_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urlreq.Request(
        flask_url.rstrip("/") + "/ingest",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            LOG.info(f"[bar→flask] {payload['bar_id']} delta={payload['delta']:.1f} → {body[:200]}")
    except Exception as e:
        LOG.warning(f"[bar→flask] POST failed: {e}")


async def run(epic: str, tf: str, flask_url: str, price_step: float, demo: bool) -> None:
    sess = login(demo=demo)
    LOG.info(f"[capital] logged in. account={sess.account_id} demo={demo}")

    builder = FootprintBuilder(
        symbol=epic,
        tf=tf,
        on_bar_close=lambda payload: _post(flask_url, payload),
        price_step=price_step,
    )
    LOG.info(f"[capital] streaming {epic} {tf} → {flask_url}/ingest (price_step={price_step})")

    def handle_quote(received_epic: str, ts_ms: int, bid: float, ask: float) -> None:
        if received_epic != epic:
            return
        builder.on_quote(ts_ms, bid, ask)

    await stream_quotes(sess, [epic], handle_quote)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epic", default="GOLD")
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--flask", default="http://localhost:5000")
    ap.add_argument("--price-step", type=float, default=0.1)
    ap.add_argument("--live", action="store_true", help="use live API base instead of demo")
    args = ap.parse_args()
    asyncio.run(run(args.epic, args.tf, args.flask, args.price_step, demo=not args.live))


if __name__ == "__main__":
    main()
