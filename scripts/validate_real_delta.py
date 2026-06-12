#!/usr/bin/env python3
"""Re-validate the delta-direction setups on REAL-delta 30-day history.

Run AFTER scripts/rebuild_footprint_binance.py has rewritten *_1m.jsonl with real
Binance aggTrade delta. Aggregates the real 1m → clean 15m (no synthetic, no dupes),
loads it into the store, and re-runs the anchor-defend + EqHL-continuation backtests
WITH the regime split — the honest test the earlier (synthetic-contaminated) run
couldn't be.

    .venv/bin/python -u scripts/validate_real_delta.py
"""
from __future__ import annotations

import json, logging, statistics, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

import scripts.delta_direction_backtest as dd
import scripts.agg_1m_to_15m as agg
from pipeline.state_store import _deserialize

S = dd.S
SYMBOLS = ["BTCUSDT", "XAUTUSDT"]


def load_clean_15m(sym):
    """Aggregate the rebuilt real 1m → 15m Bars (delta = sum of real 1m deltas)."""
    path = ROOT / "data" / "footprint" / f"{sym}_1m.jsonl"
    dicts = agg.aggregate(sym, path)
    return [_deserialize(json.dumps(d)) for d in dicts]


def stat(g):
    rs = [t["r"] for t in g]
    if not rs:
        return "n=0"
    w = [r for r in rs if r > 0]; l = [r for r in rs if r <= 0]
    pf = (sum(w) / -sum(l)) if (l and sum(l) < 0) else float("inf")
    return (f"n={len(rs):<3} WR={100*len(w)/len(rs):>3.0f}% avgR={statistics.mean(rs):>6.2f} "
            f"sumR={sum(rs):>6.1f} PF={pf:>4.2f}")


def main():
    # load real-delta 15m into the store
    spans = {}
    for sym in SYMBOLS:
        bars = load_clean_15m(sym)
        S._bars[(sym, "15m")] = bars
        if bars:
            spans[sym] = (len(bars), (bars[-1].close_ts - bars[0].close_ts) / 86400,
                          time.strftime("%m-%d", time.gmtime(bars[0].close_ts)),
                          time.strftime("%m-%d", time.gmtime(bars[-1].close_ts)))
    print("REAL-delta 15m loaded:")
    for sym, (n, d, t0, t1) in spans.items():
        print(f"  {sym}: {n} bars, {d:.1f}d [{t0}..{t1}]")
    print()

    for label, fn in [("ANCHOR-zone defend (with delta)", dd.bt_anchor),
                      ("EqHL sweep CONTINUATION (with delta)", dd.bt_eqhl_cont)]:
        print(f"=== {label} — REAL delta, 30d ===")
        allt = []
        for sym in SYMBOLS:
            dd.bt.CUT["ts"] = None
            tr = fn(sym); allt += tr
            print(f"  {sym:<9} {stat(tr)}")
        print(f"  {'ALL':<9} {stat(allt)}")
        for b in ["with_trend", "range", "counter_trend"]:
            print(f"    {b:<13} {stat([t for t in allt if t.get('bucket') == b])}")
        print()


if __name__ == "__main__":
    main()
