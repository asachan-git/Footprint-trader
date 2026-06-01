#!/usr/bin/env python3
"""Coup filter-edge report — does each filter actually separate continuation?

Reads data/strategies/coup/candidates.jsonl (from coup_backtest.py --candidates)
and prints continuation-rate splits. The honest question: do `confirmed` candidates
continue more than rejected ones? Does a higher vol_ratio / delta_ratio help? If a
split shows no separation, that filter is not earning its place.

This is also the scoreboard a future Claude-judge would be measured against: replace
`confirmed` with Claude's verdict on the same rows and compare continuation-rates.

    python scripts/coup_candidate_report.py [SYMBOL] [TF]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAND = ROOT / "data" / "strategies" / "coup" / "candidates.jsonl"


def _rate(rows) -> str:
    if not rows:
        return "  n=0"
    cont = sum(1 for r in rows if r["continuation"])
    n = len(rows)
    mfe = [r["fwd_mfe_atr"] for r in rows if r.get("fwd_mfe_atr") is not None]
    mae = [r["fwd_mae_atr"] for r in rows if r.get("fwd_mae_atr") is not None]
    avg_mfe = sum(mfe) / len(mfe) if mfe else 0.0
    avg_mae = sum(mae) / len(mae) if mae else 0.0
    return f"n={n:>3d}  cont={100*cont/n:>5.1f}%  avgMFE={avg_mfe:>4.2f}atr  avgMAE={avg_mae:>4.2f}atr"


def _split(rows, label, key_fn, buckets):
    print(f"\n{label}")
    for name, pred in buckets:
        sub = [r for r in rows if pred(key_fn(r))]
        print(f"  {name:18s} {_rate(sub)}")


def main(symbol: str | None, tf: str | None):
    if not CAND.exists():
        print(f"no candidates file at {CAND} — run: python scripts/coup_backtest.py {symbol or 'BTCUSDT'} {tf or '15m'} --candidates")
        return
    rows = [json.loads(l) for l in CAND.open() if l.strip()]
    if symbol:
        rows = [r for r in rows if r["symbol"] == symbol]
    if tf:
        rows = [r for r in rows if r["tf"] == tf]
    print(f"candidates: {len(rows)}  (continuation = winner-dir move ≥1×ATR within 6 bars)")
    print("=" * 60)
    print(f"ALL                {_rate(rows)}")

    _split(rows, "by CONFIRM filter (the edge test):", lambda r: r["confirmed"],
           [("confirmed", lambda v: v is True), ("rejected", lambda v: v is False)])
    _split(rows, "by vol_ratio:", lambda r: r["vol_ratio"],
           [("< 2.0", lambda v: v < 2.0), ("2.0 – 3.0", lambda v: 2.0 <= v < 3.0),
            (">= 3.0", lambda v: v >= 3.0)])
    _split(rows, "by delta_ratio:", lambda r: r["delta_ratio"],
           [("< 0.45", lambda v: v < 0.45), ("0.45 – 0.6", lambda v: 0.45 <= v < 0.6),
            (">= 0.6", lambda v: v >= 0.6)])
    _split(rows, "by winner side:", lambda r: r["winner"],
           [("long", lambda v: v == "long"), ("short", lambda v: v == "short")])

    conf = [r for r in rows if r["confirmed"]]
    rej = [r for r in rows if not r["confirmed"]]
    if conf and rej:
        cc = 100 * sum(1 for r in conf if r["continuation"]) / len(conf)
        rc = 100 * sum(1 for r in rej if r["continuation"]) / len(rej)
        verdict = "ADDS EDGE" if cc > rc + 5 else ("NO EDGE" if abs(cc - rc) <= 5 else "INVERTED (!)")
        print(f"\nCONFIRM filter verdict: confirmed {cc:.0f}% vs rejected {rc:.0f}% → {verdict}")
    print("\n⚠ small-n: treat splits as directional only until candidate count grows.")


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else None
    tf = sys.argv[2] if len(sys.argv) > 2 else None
    main(sym, tf)
