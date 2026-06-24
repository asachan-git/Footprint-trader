#!/usr/bin/env python3
"""Aggregate 1m footprint JSONL → synthetic 15m footprint JSONL.

Aggregation rules:
  OHLC  : open=first, high=max, low=min, close=last
  delta : sum of 1m deltas
  ladder: merge price levels across all 1m bars in window, sum volumes
  poc   : price level with highest total (bid+ask) volume in merged ladder
  bar_id: SYMBOL|15m|close_ts
  tf    : "15m"

Usage:
    python scripts/agg_1m_to_15m.py --symbol XAUTUSDT
    python scripts/agg_1m_to_15m.py --symbol BTCUSDT --out data/footprint/BTCUSDT_15m_agg.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FP_DIR = ROOT / "data" / "footprint"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


_TF_SEC = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}


def bucket_ts(close_ts: int, tf_sec: int = 900) -> int:
    """Target-TF bucket a 1m bar belongs to: ceil(close_ts / tf_sec) * tf_sec."""
    return math.ceil(close_ts / tf_sec) * tf_sec


def merge_ladders(bars: list[dict]) -> tuple[list[dict], list[dict], float | None]:
    """Merge bid/ask ladders across bars, sum volumes at each price level."""
    bid: dict[float, float] = defaultdict(float)
    ask: dict[float, float] = defaultdict(float)
    for b in bars:
        for lvl in b.get("bid_ladder") or []:
            bid[lvl["price"]] += lvl["vol"]
        for lvl in b.get("ask_ladder") or []:
            ask[lvl["price"]] += lvl["vol"]
    all_prices = sorted(set(bid) | set(ask))
    # POC: price with max total volume
    poc = max(all_prices, key=lambda p: bid.get(p, 0) + ask.get(p, 0)) if all_prices else None
    bid_ladder = [{"price": p, "vol": round(bid[p], 6)} for p in sorted(bid)]
    ask_ladder = [{"price": p, "vol": round(ask[p], 6)} for p in sorted(ask)]
    return bid_ladder, ask_ladder, poc


def aggregate(symbol: str, input_path: Path, target_tf: str = "15m") -> list[dict]:
    tf_sec = _TF_SEC[target_tf]
    bars_1m = [b for b in load_jsonl(input_path) if b.get("symbol") == symbol and b.get("tf") == "1m"]
    bars_1m.sort(key=lambda b: b["close_ts"])

    # Group into target-TF buckets
    buckets: dict[int, list[dict]] = defaultdict(list)
    for b in bars_1m:
        buckets[bucket_ts(b["close_ts"], tf_sec)].append(b)

    out: list[dict] = []
    for bts in sorted(buckets):
        group = sorted(buckets[bts], key=lambda b: b["close_ts"])
        if not group:
            continue
        bid_ladder, ask_ladder, poc = merge_ladders(group)
        delta = sum(b.get("delta") or 0 for b in group)
        highs = [b["ohlc"]["h"] for b in group]
        lows  = [b["ohlc"]["l"] for b in group]
        bar = {
            "bar_id":     f"{symbol}|{target_tf}|{bts}",
            "symbol":     symbol,
            "tf":         target_tf,
            "close_ts":   bts,
            "source":     "agg_1m",
            "ohlc": {
                "o": group[0]["ohlc"]["o"],
                "h": max(highs),
                "l": min(lows),
                "c": group[-1]["ohlc"]["c"],
            },
            "bid_ladder": bid_ladder,
            "ask_ladder": ask_ladder,
            "poc":        poc,
            "delta":      round(delta, 6),
        }
        out.append(bar)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", "-s", required=True)
    parser.add_argument("--tf", default="15m", choices=sorted(_TF_SEC),
                        help="Target timeframe to aggregate 1m bars into")
    parser.add_argument("--data-dir", default=str(FP_DIR))
    parser.add_argument("--out", default=None,
                        help="Output path (default: FP_DIR/SYMBOL_15m_agg.jsonl for 15m, "
                             "else canonical FP_DIR/SYMBOL_TF.jsonl)")
    parser.add_argument("--print-stats", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    input_path = data_dir / f"{args.symbol}_1m.jsonl"
    if args.out:
        out_path = Path(args.out)
    elif args.tf == "15m":
        out_path = data_dir / f"{args.symbol}_15m_agg.jsonl"   # back-compat default
    else:
        out_path = data_dir / f"{args.symbol}_{args.tf}.jsonl"  # canonical (state_store reads this)

    print(f"[agg] reading {input_path} ...")
    bars = aggregate(args.symbol, input_path, args.tf)
    print(f"[agg] aggregated → {len(bars)} {args.tf} bars")

    if bars and args.print_stats:
        from datetime import datetime, timezone
        t0 = datetime.fromtimestamp(bars[0]["close_ts"], tz=timezone.utc)
        t1 = datetime.fromtimestamp(bars[-1]["close_ts"], tz=timezone.utc)
        print(f"[agg] range: {t0} → {t1}")

    with out_path.open("w") as fh:
        for b in bars:
            fh.write(json.dumps(b) + "\n")
    print(f"[agg] written → {out_path}")


if __name__ == "__main__":
    main()
