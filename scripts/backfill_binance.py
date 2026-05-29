"""Backfill Binance aggTrades history → real footprint bars in state_store.

Unlike fetch_history.py (OHLCV synthetic), this replays actual trades through
FootprintBuilder producing genuine bid/ask ladders per bar — real order flow data.

Binance aggTrades endpoint: GET /fapi/v1/aggTrades
  - Fields: T (ts_ms), p (price), q (qty), m (isBuyerMaker)
  - 1000 trades per page, paginate via startTime/endTime
  - Goes back 30+ days

Usage:
  python3 scripts/backfill_binance.py                           # XAUUSDT→XAUTUSDT, 7 days
  python3 scripts/backfill_binance.py --days 14
  python3 scripts/backfill_binance.py --symbol XAUUSDT --symbol-as XAUTUSDT --days 7
  python3 scripts/backfill_binance.py --symbol BTCUSDT --days 5
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bybit.footprint_builder import FootprintBuilder
from pipeline.state_store import store
from pipeline.features.vp_cache import build_and_save

IST_TZ = timezone(timedelta(hours=5, minutes=30))
BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/aggTrades"


def _load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def fetch_agg_trades(
    symbol: str,
    start_ms: int,
    end_ms: int,
    page_size: int = 1000,
) -> list[dict]:
    """Fetch all aggTrades for symbol between start_ms and end_ms."""
    trades = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            resp = requests.get(
                BINANCE_FUTURES_URL,
                params={"symbol": symbol, "startTime": cursor, "endTime": min(cursor + 3_600_000, end_ms), "limit": page_size},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"  API error at {cursor}: {e}; retrying in 3s")
            time.sleep(3)
            continue

        if not batch:
            cursor += 3_600_000
            continue

        trades.extend(batch)
        last_ts = int(batch[-1]["T"])
        cursor = last_ts + 1
        time.sleep(0.05)  # stay under rate limit

    return trades


def backfill(
    binance_symbol: str,
    store_symbol: str,
    days: int,
    tf: str,
    price_step: float,
) -> int:
    """Fetch aggTrades and replay through FootprintBuilder → state_store.

    Returns count of bars injected.
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000

    print(f"  Symbol: {binance_symbol} → stored as {store_symbol}")
    print(f"  Range:  {datetime.fromtimestamp(start_ms/1000, tz=IST_TZ).strftime('%Y-%m-%d %H:%M IST')} → now")
    print(f"  TF: {tf}, price_step: {price_step}")

    s = store()
    bars_injected = 0

    def _on_bar_close(payload: dict) -> None:
        nonlocal bars_injected
        from pipeline.types import Bar, Level, OHLC
        ohlc = payload["ohlc"]
        bar = Bar(
            bar_id=payload["bar_id"].replace(binance_symbol, store_symbol, 1),
            symbol=store_symbol,
            tf=payload["tf"],
            close_ts=int(payload["close_ts"]),
            source="binance_agg_backfill",
            ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
            bid_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("bid_ladder", [])),
            ask_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"])) for l in payload.get("ask_ladder", [])),
            poc=payload.get("poc"),
            delta=float(payload["delta"]) if payload.get("delta") is not None else None,
        )
        if s.put(bar):
            bars_injected += 1

    builder = FootprintBuilder(
        symbol=binance_symbol,
        tf=tf,
        on_bar_close=_on_bar_close,
        price_step=price_step,
    )

    # Fetch in 1-day chunks to show progress
    day_ms = 86_400_000
    cursor = start_ms
    total_trades = 0
    while cursor < now_ms:
        chunk_end = min(cursor + day_ms, now_ms)
        date_label = datetime.fromtimestamp(cursor / 1000, tz=IST_TZ).strftime("%Y-%m-%d")
        print(f"  Fetching {date_label}...", end=" ", flush=True)
        trades = fetch_agg_trades(binance_symbol, cursor, chunk_end)
        total_trades += len(trades)
        print(f"{len(trades)} trades")
        for t in trades:
            side = "Sell" if t["m"] else "Buy"
            builder.on_tick(int(t["T"]), float(t["p"]), float(t["q"]), side)
        cursor = chunk_end

    # Flush any partial bar
    builder.flush()

    print(f"  Total trades processed: {total_trades:,}")
    print(f"  Bars injected: {bars_injected}")
    return bars_injected


def main() -> None:
    settings = _load_settings()
    vp_cfg = settings.get("vp_cache", {})

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSDT", help="Binance futures symbol to fetch")
    ap.add_argument("--symbol-as", default=None,
                    help="Symbol name to store under (default: same as --symbol)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tf", default="1m", choices=["1m", "5m", "15m"])
    ap.add_argument("--price-step", type=float, default=0.1,
                    help="Footprint price bucket size (XAU tick = 0.1)")
    ap.add_argument("--no-vp-rebuild", action="store_true",
                    help="Skip VP cache rebuild after backfill")
    args = ap.parse_args()

    store_symbol = args.symbol_as or args.symbol

    print(f"\n=== Binance aggTrade backfill ===")
    n = backfill(
        binance_symbol=args.symbol,
        store_symbol=store_symbol,
        days=args.days,
        tf=args.tf,
        price_step=args.price_step,
    )

    if n > 0 and not args.no_vp_rebuild:
        print(f"\nRebuilding VP cache for {store_symbol}...")
        sym_map = (settings.get("execution") or {}).get("symbol_map", {})
        build_and_save(
            [store_symbol],
            args.tf,
            session_start_utc=vp_cfg.get("session_start_utc", {}),
            vp_bin_size=vp_cfg.get("vp_bin_size", {}),
            venue_price_offset=vp_cfg.get("venue_price_offset", {}),
            symbol_map=sym_map,
        )
        print("VP cache rebuilt.")
    elif n == 0:
        print("No new bars — cache not updated.")


if __name__ == "__main__":
    main()
