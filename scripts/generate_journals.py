"""Backfill daily bar journals for all available historical data.

Usage:
    python3 scripts/generate_journals.py                  # all symbols, all days
    python3 scripts/generate_journals.py --days 7         # last 7 days
    python3 scripts/generate_journals.py --symbol BTCUSDT # one symbol only
    python3 scripts/generate_journals.py --symbol BTCUSDT --days 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_IST = timezone(timedelta(hours=5, minutes=30))

SYMBOL_CONFIG = {
    "BTCUSDT":  {"session_start_utc": 0,  "primary_tf": "1m"},
    "XAUTUSDT": {"session_start_utc": 22, "primary_tf": "1m"},
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily bar journals")
    parser.add_argument("--days",   type=int, default=30, help="How many days back to generate")
    parser.add_argument("--symbol", type=str, default=None, help="Single symbol (default: all)")
    args = parser.parse_args()

    from pipeline.features.daily_journal import write_day_journal
    from pipeline.features.vp_cache import _session_day_key

    symbols = [args.symbol] if args.symbol else list(SYMBOL_CONFIG.keys())
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())

    print(f"Generating journals for {symbols}, last {args.days} days…\n")
    total = 0

    for symbol in symbols:
        cfg = SYMBOL_CONFIG.get(symbol, {"session_start_utc": 0, "primary_tf": "1m"})
        sess_utc = cfg["session_start_utc"]
        primary_tf = cfg["primary_tf"]

        generated: set[str] = set()
        for days_ago in range(1, args.days + 1):
            ts = now_ts - days_ago * 86400
            date_key = _session_day_key(ts, sess_utc)
            if date_key in generated:
                continue
            generated.add(date_key)

            out_path = ROOT / "data" / "journal" / symbol / f"{date_key}.md"
            if out_path.exists():
                print(f"  {symbol} {date_key}  [exists, skipping]")
                continue

            result = write_day_journal(symbol, primary_tf, date_key, sess_utc)
            if result:
                print(f"  {symbol} {date_key}  → {result.name}")
                total += 1
            else:
                print(f"  {symbol} {date_key}  [insufficient data]")

    print(f"\nDone — {total} journal(s) generated.")
    print(f"View at: {ROOT / 'data' / 'journal'}")


if __name__ == "__main__":
    main()
