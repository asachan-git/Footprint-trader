#!/usr/bin/env python3
"""squeeze_ab.py — compare grid-cycle outcomes WITH vs WITHOUT the squeeze condition.

Reads data/exec_emit.jsonl (the ground-truth arm/exit audit). Every arm and every
cycle exit is stamped `squeeze_ok` (was vol coiled at arm time — BB 3σ BBW-percentile,
the same test the gate enforces). With require_squeeze_gate=false the emitter arms BOTH
coiled and uncoiled grids, so this buckets the realized outcomes squeeze-pass vs -fail.

Outcome per cycle = the flatten/exit row's `pnl` (basket floating $ at CLOSE_ALL) — an
approximation of realized cycle P&L (good enough to rank the two buckets). exit_reason
also tells the shape: net_target = booked green, full_hedge = loss-cut, leg_tp = clean.

Usage:
  PYTHONPATH=. venv/bin/python scripts/squeeze_ab.py [--symbol BTCUSD] [--tf 5m] [--since 2026-06-21]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "data" / "exec_emit.jsonl"
EXIT_REASONS = {"net_target", "full_hedge", "leg_tp", "leg_closed_other", "bias_book_trail"}


def _rows(symbol: str | None, tf: str | None, since_ts: float):
    arms, exits = [], []
    if not LOG.exists():
        return arms, exits
    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ts", 0) < since_ts:
            continue
        sym = r.get("broker_symbol") or r.get("symbol") or ""
        if symbol and symbol not in (sym, r.get("symbol")):
            continue
        if tf and r.get("tf") != tf:
            continue
        if r.get("verdict") == "arm":
            arms.append(r)
        elif r.get("exit_reason") in EXIT_REASONS:
            exits.append(r)
    return arms, exits


def _bucket(rows: list[dict], key: str):
    """Group rows by squeeze_ok (True/False/None); return per-bucket stats on `key` ($)."""
    out = {}
    by = defaultdict(list)
    for r in rows:
        by[r.get("squeeze_ok")].append(r)
    for sq, rs in by.items():
        vals = [float(r.get(key) or 0.0) for r in rs]
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gross_w, gross_l = sum(wins), abs(sum(losses))
        out[sq] = {
            "n": len(rs),
            "total": round(sum(vals), 2),
            "avg": round(sum(vals) / len(rs), 3) if rs else 0.0,
            "win_rate": round(len(wins) / len(rs), 3) if rs else 0.0,
            "pf": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
            "reasons": dict(sorted(
                ((k, sum(1 for r in rs if r.get("exit_reason") == k))
                 for k in {r.get("exit_reason") for r in rs}), key=lambda x: -x[1])),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="broker or analysis symbol filter (e.g. BTCUSD)")
    ap.add_argument("--tf", default=None, help="timeframe filter (e.g. 5m)")
    ap.add_argument("--since", default=None, help="only rows on/after this date (YYYY-MM-DD, local)")
    args = ap.parse_args()

    since_ts = 0.0
    if args.since:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    arms, exits = _rows(args.symbol, args.tf, since_ts)

    def lbl(sq):
        return {True: "squeeze ✓", False: "squeeze ✗", None: "unlabeled"}.get(sq, str(sq))

    print(f"\nSqueeze A/B — exec_emit.jsonl  (symbol={args.symbol or 'all'} tf={args.tf or 'all'} "
          f"since={args.since or 'all'})")
    print("=" * 72)

    # arm-rate split
    arm_by = defaultdict(int)
    for r in arms:
        arm_by[r.get("squeeze_ok")] += 1
    print(f"ARMS: {len(arms)}   " + "  ".join(f"{lbl(k)}={v}" for k, v in arm_by.items()))

    if not exits:
        print("\nNo cycle exits recorded yet — let some grids run, then re-run.\n")
        return

    print(f"\nCYCLE OUTCOMES (n={len(exits)}; outcome = basket $ at flatten):\n")
    stats = _bucket(exits, "pnl")
    hdr = f"{'bucket':12} {'n':>4} {'total$':>9} {'avg$':>8} {'win%':>6} {'PF':>6}   reasons"
    print(hdr); print("-" * len(hdr))
    for sq in (True, False, None):
        if sq not in stats:
            continue
        s = stats[sq]
        print(f"{lbl(sq):12} {s['n']:>4} {s['total']:>9.2f} {s['avg']:>8.3f} "
              f"{s['win_rate']*100:>5.1f}% {s['pf']:>6}   {s['reasons']}")

    a, b = stats.get(True), stats.get(False)
    if a and b:
        print("\nVERDICT:")
        print(f"  squeeze-pass avg ${a['avg']:.3f} (PF {a['pf']})  vs  "
              f"squeeze-fail avg ${b['avg']:.3f} (PF {b['pf']})")
        better = "squeeze GATE helps — enforce it" if a["avg"] > b["avg"] else \
                 "squeeze gate does NOT help on this sample — keep observing / drop it"
        print(f"  → {better}")
    print()


if __name__ == "__main__":
    main()
