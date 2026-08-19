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

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.features.vp_cache import build_and_save
# Shared backfill core (also used by the live self-heal path in pipeline/feed_monitor.py).
from binance.backfill import backfill_window, fetch_agg_trades  # noqa: F401 (re-export for callers)

IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


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

    # Delegate to the shared window backfill (binance/backfill.py), one day per call so the
    # per-day progress line is preserved. Each day flushes its own partial bar; the last day's
    # partial gets re-emitted on the next boundary anyway (store.put is idempotent), and the
    # final call flushes the trailing bar. Note: a bar spanning a day boundary is split across
    # two window calls — harmless, store.put dedups the shared boundary minute.
    day_ms = 86_400_000
    cursor = start_ms
    bars_injected = 0
    while cursor < now_ms:
        chunk_end = min(cursor + day_ms, now_ms)
        date_label = datetime.fromtimestamp(cursor / 1000, tz=IST_TZ).strftime("%Y-%m-%d")
        print(f"  Fetching {date_label}...", end=" ", flush=True)
        n = backfill_window(binance_symbol, store_symbol, cursor, chunk_end,
                            tf=tf, price_step=price_step)
        bars_injected += n
        print(f"{n} bars")
        cursor = chunk_end
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
