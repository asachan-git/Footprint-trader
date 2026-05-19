"""Rebuild real footprint history from Bybit public trade files.

Downloads daily gzip CSV files from public.bybit.com/trading/{SYMBOL}/,
aggregates every trade into 1m bid/ask ladders, and replaces existing
synthetic_ohlcv bars with real footprint bars in data/footprint/ JSONL files.

Coverage: BTCUSDT linear (May 14-18, ~287 MB compressed).
XAUTUSDT spot is not available in Bybit's public bulk data — Kaufman delta
approximation is used for those historical bars.

Usage:
    python3 scripts/rebuild_footprint_history.py
    python3 scripts/rebuild_footprint_history.py --days 3   (last 3 days only)
    python3 scripts/rebuild_footprint_history.py --dry-run  (download but don't write)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "footprint"
CACHE_DIR = ROOT / "data" / "trade_cache"

BASE_URL = "https://public.bybit.com/trading"
PRICE_STEP = 10.0   # $10 cells for BTCUSDT
IST = timezone(timedelta(hours=5, minutes=30))

SYMBOLS = ["BTCUSDT"]   # XAUTUSDT not available in Bybit public bulk data


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    import hashlib
    key = f"{symbol}|{tf}|{close_ts}"
    return f"{symbol}|{tf}|{close_ts}|{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def _round_price(price: float, step: float = PRICE_STEP) -> float:
    return round(round(price / step) * step, 8)


def download_day(symbol: str, date_str: str) -> Path | None:
    """Download and cache a single day's trade file. Returns local path."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / f"{symbol}_{date_str}.csv.gz"
    if local.exists():
        print(f"  [cache] {local.name}")
        return local

    url = f"{BASE_URL}/{symbol}/{symbol}{date_str}.csv.gz"
    print(f"  [download] {url} ...", end=" ", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FootprintBiot/1.0"})
        resp = urllib.request.urlopen(req, timeout=60)
        data = resp.read()
        local.write_bytes(data)
        mb = len(data) / 1e6
        print(f"{mb:.1f} MB ✓")
        return local
    except Exception as e:
        print(f"SKIP ({e})")
        return None


def aggregate_day(
    gz_path: Path,
    symbol: str,
    price_step: float = PRICE_STEP,
) -> dict[int, dict]:
    """Parse a day's trade file → dict of {close_ts_1m: bar_data}.

    Each bar_data:
      ohlc: {o, h, l, c}
      bid_ladder: {price: vol}   — taker sells hit bids
      ask_ladder: {price: vol}   — taker buys lift asks
      delta: float
      trade_count: int
    """
    bars: dict[int, dict] = {}

    with gzip.open(gz_path, "rt", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts_raw = float(row["timestamp"])
                price = float(row["price"])
                size = float(row["size"])
                side = row["side"]  # "Buy" | "Sell"
            except (KeyError, ValueError):
                continue

            # 1m bar boundary (ceiling)
            bar_ts = math.ceil(ts_raw / 60) * 60
            rounded_price = _round_price(price, price_step)

            if bar_ts not in bars:
                bars[bar_ts] = {
                    "o": price, "h": price, "l": price, "c": price,
                    "bid_ladder": defaultdict(float),
                    "ask_ladder": defaultdict(float),
                    "delta": 0.0,
                    "trade_count": 0,
                    "first_ts": ts_raw,
                }
            b = bars[bar_ts]
            b["h"] = max(b["h"], price)
            b["l"] = min(b["l"], price)
            b["c"] = price
            b["trade_count"] += 1

            # Taker buy = aggressor lifted ask → goes to ask_ladder
            # Taker sell = aggressor hit bid → goes to bid_ladder
            if side == "Buy":
                b["ask_ladder"][rounded_price] += size
                b["delta"] += size
            else:
                b["bid_ladder"][rounded_price] += size
                b["delta"] -= size

    # Freeze defaultdicts
    for b in bars.values():
        b["bid_ladder"] = dict(b["bid_ladder"])
        b["ask_ladder"] = dict(b["ask_ladder"])

    return bars


def bars_to_jsonl(symbol: str, bars: dict[int, dict]) -> list[str]:
    """Convert aggregated bar data to JSONL-serialisable dicts."""
    lines = []
    for close_ts in sorted(bars):
        b = bars[close_ts]
        bid = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["bid_ladder"].items())]
        ask = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["ask_ladder"].items())]
        total_vol = sum(v for v in b["bid_ladder"].values()) + sum(v for v in b["ask_ladder"].values())
        lines.append(json.dumps({
            "bar_id":      _bar_id(symbol, "1m", close_ts),
            "symbol":      symbol,
            "tf":          "1m",
            "close_ts":    close_ts,
            "source":      "bybit_tick_rebuilt",
            "ohlc":        {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]},
            "bid_ladder":  bid,
            "ask_ladder":  ask,
            "poc":         None,
            "delta":       round(b["delta"], 6),
            "trade_count": b["trade_count"],
            "total_vol":   round(total_vol, 6),
        }))
    return lines


def patch_jsonl(symbol: str, new_bars: dict[int, dict], dry_run: bool = False) -> dict:
    """Replace synthetic_ohlcv entries in the stored JSONL with real tick data."""
    fpath = DATA_DIR / f"{symbol}_1m.jsonl"
    if not fpath.exists():
        return {"status": "no_file"}

    existing = [json.loads(l) for l in fpath.read_text().splitlines() if l.strip()]

    # Index new bars by close_ts for fast lookup
    real_bar_map: dict[int, str] = {}
    for close_ts, b in new_bars.items():
        bid = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["bid_ladder"].items())]
        ask = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["ask_ladder"].items())]
        real_bar_map[close_ts] = json.dumps({
            "bar_id":      _bar_id(symbol, "1m", close_ts),
            "symbol":      symbol,
            "tf":          "1m",
            "close_ts":    close_ts,
            "source":      "bybit_tick_rebuilt",
            "ohlc":        {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]},
            "bid_ladder":  bid,
            "ask_ladder":  ask,
            "poc":         None,
            "delta":       round(b["delta"], 6),
            "trade_count": b["trade_count"],
        })

    replaced = kept_synthetic = kept_live = new_added = 0
    out_lines = []

    existing_ts = {e["close_ts"] for e in existing}

    # Replace / keep existing bars
    for e in existing:
        cts = e["close_ts"]
        if cts in real_bar_map:
            out_lines.append(real_bar_map[cts])
            replaced += 1
        else:
            out_lines.append(json.dumps(e))
            if e.get("source") == "synthetic_ohlcv":
                kept_synthetic += 1
            else:
                kept_live += 1

    # Add real bars that didn't exist at all (gaps in synthetic history)
    for cts, line in real_bar_map.items():
        if cts not in existing_ts:
            out_lines.append(line)
            new_added += 1

    if not dry_run:
        # Sort by close_ts before writing
        out_lines.sort(key=lambda l: json.loads(l)["close_ts"])
        fpath.write_text("\n".join(out_lines) + "\n")

    return {
        "replaced": replaced,
        "kept_synthetic": kept_synthetic,
        "kept_live": kept_live,
        "new_added": new_added,
        "total": len(out_lines),
    }


def run(days: int = 7, dry_run: bool = False, symbols: list[str] | None = None) -> None:
    targets = symbols or SYMBOLS
    today = datetime.now(tz=timezone.utc).date()

    print(f"\n{'='*60}")
    print(f"Rebuilding real footprint history")
    print(f"  Symbols:  {targets}")
    print(f"  Days:     last {days}")
    print(f"  Dry-run:  {dry_run}")
    print(f"{'='*60}\n")

    for symbol in targets:
        price_step = 10.0 if "BTC" in symbol else 0.1
        print(f"\n── {symbol} ──")
        all_bars: dict[int, dict] = {}

        for days_ago in range(2, days + 2):   # skip today + yesterday (not published yet)
            date_str = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            gz = download_day(symbol, date_str)
            if gz is None:
                continue
            print(f"  [parse]  {date_str} ...", end=" ", flush=True)
            t0 = time.time()
            day_bars = aggregate_day(gz, symbol, price_step)
            print(f"{len(day_bars)} bars in {time.time()-t0:.1f}s")
            all_bars.update(day_bars)

        if not all_bars:
            print(f"  No data available for {symbol}")
            continue

        ts_range = f"{_ist(min(all_bars))} → {_ist(max(all_bars))}"
        print(f"  [total]  {len(all_bars)} 1m bars  {ts_range}")

        result = patch_jsonl(symbol, all_bars, dry_run=dry_run)
        action = "(dry-run)" if dry_run else "→ written"
        print(f"  [patch]  {action}")
        print(f"    replaced:       {result['replaced']:>6}  synthetic→real")
        print(f"    kept synthetic: {result['kept_synthetic']:>6}  (outside tick range)")
        print(f"    kept live:      {result['kept_live']:>6}  (WebSocket bars unchanged)")
        print(f"    new added:      {result['new_added']:>6}  (gaps filled)")
        print(f"    total bars:     {result['total']:>6}")

    print(f"\n{'='*60}")
    if not dry_run:
        print("Done. Restart server to reload: bash scripts/start.sh")
    else:
        print("Dry-run complete — no files written.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild footprint history from Bybit tick data")
    parser.add_argument("--days",    type=int,  default=7,     help="How many days back to download")
    parser.add_argument("--dry-run", action="store_true",      help="Download + parse but don't write")
    parser.add_argument("--symbol",  action="append", dest="symbols", help="Override symbol list")
    args = parser.parse_args()
    run(days=args.days, dry_run=args.dry_run, symbols=args.symbols)
