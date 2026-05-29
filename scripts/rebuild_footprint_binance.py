"""Rebuild footprint history from Binance Futures public aggTrades dumps.

Pulls daily aggTrade zips from data.binance.vision/data/futures/um/daily/
for a given symbol, buckets every tick into 1m bid/ask ladders, then
replaces matching entries in data/footprint/<symbol_as>_1m.jsonl.

Usage:
    python3 scripts/rebuild_footprint_binance.py
        --symbol XAUUSDT --symbol-as XAUTUSDT --price-step 0.1 --days 7

    python3 scripts/rebuild_footprint_binance.py
        --symbol BTCUSDT --price-step 10.0 --days 7

CSV cols (Binance Futures aggTrades):
    agg_trade_id, price, quantity, first_trade_id, last_trade_id,
    transact_time, is_buyer_maker
m=true  -> seller is taker -> bid_ladder
m=false -> buyer is taker  -> ask_ladder
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT      = Path(__file__).resolve().parent.parent
DATA_DIR  = ROOT / "data" / "footprint"
CACHE_DIR = ROOT / "data" / "trade_cache_binance"

BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
IST      = timezone(timedelta(hours=5, minutes=30))


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    key = f"{symbol}|{tf}|{close_ts}"
    return f"{symbol}|{tf}|{close_ts}|{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def _round_price(price: float, step: float) -> float:
    return round(round(price / step) * step, 8)


def download_day(symbol: str, date_str: str) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / f"{symbol}_{date_str}.zip"
    if local.exists() and local.stat().st_size > 0:
        return local
    url = f"{BASE_URL}/{symbol}/{symbol}-aggTrades-{date_str}.zip"
    print(f"  fetch {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FootprintBiot/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if not data:
            return None
        local.write_bytes(data)
        return local
    except Exception as e:
        print(f"  ERROR fetch {date_str}: {e}")
        return None


def iter_trades(zip_path: Path):
    """Yield (ts_ms, price, qty, m) per trade from the daily zip."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
                reader = csv.reader(text)
                first = next(reader, None)
                if first is None:
                    return
                # Some Binance dumps have headers, some don't. Detect by trying float.
                rows_iter = reader
                try:
                    _ = int(first[0])
                    # First row is data; replay it
                    rows_iter = ([first] + list(reader)).__iter__()
                except Exception:
                    pass  # header row, skip
                for row in rows_iter:
                    if len(row) < 7:
                        continue
                    try:
                        price = float(row[1])
                        qty   = float(row[2])
                        ts_ms = int(row[5])
                        m     = row[6].strip().lower() in ("true", "1")
                        yield ts_ms, price, qty, m
                    except Exception:
                        continue


def aggregate_to_bars(trades_iter, price_step: float) -> dict[int, dict]:
    bars: dict[int, dict] = {}
    for ts_ms, price, qty, is_buyer_maker in trades_iter:
        close_ts = (ts_ms // 1000 // 60 + 1) * 60   # 1m bar close epoch (seconds)
        b = bars.get(close_ts)
        if b is None:
            b = bars[close_ts] = {
                "o": price, "h": price, "l": price, "c": price,
                "bid_ladder": defaultdict(float),
                "ask_ladder": defaultdict(float),
                "delta": 0.0,
                "trade_count": 0,
            }
        b["h"] = max(b["h"], price)
        b["l"] = min(b["l"], price)
        b["c"] = price
        b["trade_count"] += 1
        rp = _round_price(price, price_step)
        if is_buyer_maker:                         # seller-aggressor → bid side
            b["bid_ladder"][rp] += qty
            b["delta"] -= qty
        else:                                      # buyer-aggressor  → ask side
            b["ask_ladder"][rp] += qty
            b["delta"] += qty
    for b in bars.values():
        b["bid_ladder"] = dict(b["bid_ladder"])
        b["ask_ladder"] = dict(b["ask_ladder"])
    return bars


def _serialize_bar(symbol_as: str, close_ts: int, b: dict) -> str:
    bid = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["bid_ladder"].items())]
    ask = [{"price": p, "vol": round(v, 6)} for p, v in sorted(b["ask_ladder"].items())]
    return json.dumps({
        "bar_id":      _bar_id(symbol_as, "1m", close_ts),
        "symbol":      symbol_as,
        "tf":          "1m",
        "close_ts":    close_ts,
        "source":      "binance_futures_rebuilt",
        "ohlc":        {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]},
        "bid_ladder":  bid,
        "ask_ladder":  ask,
        "poc":         None,
        "delta":       round(b["delta"], 6),
        "trade_count": b["trade_count"],
    })


def patch_jsonl(symbol_as: str, new_bars: dict[int, dict], dry_run: bool = False) -> dict:
    fpath = DATA_DIR / f"{symbol_as}_1m.jsonl"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    real_map = {cts: _serialize_bar(symbol_as, cts, b) for cts, b in new_bars.items()}

    if not fpath.exists():
        if not dry_run:
            lines = [real_map[c] for c in sorted(real_map)]
            fpath.write_text("\n".join(lines) + "\n")
        return {"status": "fresh", "written": len(real_map)}

    existing = [json.loads(l) for l in fpath.read_text().splitlines() if l.strip()]
    existing_ts = {e["close_ts"] for e in existing}
    replaced = kept = added = 0
    out_lines = []
    for e in existing:
        cts = e["close_ts"]
        if cts in real_map:
            out_lines.append(real_map[cts])
            replaced += 1
        else:
            out_lines.append(json.dumps(e))
            kept += 1
    for cts in real_map:
        if cts not in existing_ts:
            out_lines.append(real_map[cts])
            added += 1
    if not dry_run:
        out_lines.sort(key=lambda l: json.loads(l)["close_ts"])
        fpath.write_text("\n".join(out_lines) + "\n")
    return {"replaced": replaced, "kept": kept, "added": added, "total": len(out_lines)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol",      required=True,
                    help="Binance Futures symbol, e.g. BTCUSDT, XAUUSDT")
    ap.add_argument("--symbol-as",   default=None,
                    help="Remap symbol in stored data (e.g. XAUUSDT → XAUTUSDT)")
    ap.add_argument("--price-step",  type=float, required=True,
                    help="Ladder price-bucket size (must match live feed)")
    ap.add_argument("--days",        type=int,   default=7,
                    help="How many days back to download (default 7)")
    ap.add_argument("--dry-run",     action="store_true")
    args = ap.parse_args()

    symbol_as = args.symbol_as or args.symbol

    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(1, args.days + 1)]

    all_bars: dict[int, dict] = {}
    t0 = time.time()
    for date_str in dates:
        zp = download_day(args.symbol, date_str)
        if not zp:
            continue
        print(f"  parse {date_str}")
        day_bars = aggregate_to_bars(iter_trades(zp), args.price_step)
        # Merge into all_bars (later wins on conflict)
        for ts, b in day_bars.items():
            all_bars[ts] = b

    print(f"Aggregated {len(all_bars)} 1m bars across {args.days} days "
          f"in {time.time() - t0:.1f}s")

    result = patch_jsonl(symbol_as, all_bars, dry_run=args.dry_run)
    print(f"Patch {symbol_as}_1m.jsonl: {result}")


if __name__ == "__main__":
    main()
