#!/usr/bin/env python3
"""Tier 0.3 — aggregate cycle + position stats.

Answers:
  - % cycles single-leg (grid never DCA'd)
  - close reason breakdown (sl_hit / tp_hit / cycle_tp / tp_absorption / other)
  - hedge fire rate (child cycles with parent_cycle_id)
  - per-symbol R distribution
  - avg cycle duration in seconds
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYCLES = ROOT / "data" / "cycles.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"


def classify_reason(raw: str) -> str:
    s = raw.lower()
    if s.startswith("sl_hit"):
        return "sl_hit"
    if s.startswith("tp_hit"):
        return "tp_hit"
    if s.startswith("cycle_tp"):
        return "cycle_tp"
    if "absorption" in s:
        return "tp_absorption"
    if "invalid" in s or "choch" in s:
        return "invalidation"
    if "trend" in s or "escape" in s:
        return "trend_escape"
    return f"other:{raw[:30]}"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.open() if l.strip()]


def main() -> None:
    cycles = load_jsonl(CYCLES)
    positions = load_jsonl(POSITIONS)

    # cycle index
    cycle_open: dict[str, dict] = {}
    cycle_close: dict[str, dict] = {}
    parents: list[str] = []
    for ev in cycles:
        cid = ev.get("cycle_id")
        if ev["type"] == "open":
            cycle_open[cid] = ev
            if ev.get("parent_cycle_id"):
                parents.append(ev["parent_cycle_id"])
        elif ev["type"] == "close":
            cycle_close[cid] = ev

    # position index: per position_id, count add_leg, find close reason + pnl
    leg_count: dict[str, int] = defaultdict(lambda: 1)  # leg 1 = open
    pos_close: dict[str, dict] = {}
    pos_open: dict[str, dict] = {}
    for ev in positions:
        pid = ev.get("position_id")
        if not pid:
            continue
        t = ev["type"]
        if t == "open":
            pos_open[pid] = ev
        elif t == "add_leg":
            leg_count[pid] += 1
        elif t == "close":
            pos_close[pid] = ev

    n_cycles = len(cycle_open)
    n_closed = len(cycle_close)
    n_with_pnl = sum(1 for c in cycle_close.values() if c.get("realized_pnl") is not None)
    pnls = [c["realized_pnl"] for c in cycle_close.values() if c.get("realized_pnl") is not None]
    n_hedges = len(set(parents))  # cycles that fired a hedge child

    # join: cycle_id -> position_id (via cycle open ev) -> leg_count
    legs_per_cycle = []
    durations = []
    reasons = Counter()
    per_symbol_r: dict[str, list[float]] = defaultdict(list)
    per_symbol_legs: dict[str, list[int]] = defaultdict(list)

    for cid, co in cycle_open.items():
        cc = cycle_close.get(cid)
        if not cc:
            continue
        pid = co.get("position_id")
        legs = leg_count.get(pid, 1) if pid else 1
        legs_per_cycle.append(legs)
        per_symbol_legs[co["symbol"]].append(legs)

        dur = cc["ts"] - co["ts"]
        durations.append(dur)

        if cc.get("realized_pnl") is not None:
            per_symbol_r[co["symbol"]].append(cc["realized_pnl"])

        reasons[classify_reason(cc.get("reason", "unknown"))] += 1

    # output
    print("=" * 60)
    print("CYCLE STATS")
    print("=" * 60)
    print(f"Total cycles opened : {n_cycles}")
    print(f"Total cycles closed : {n_closed} ({n_closed/n_cycles*100:.1f}%)" if n_cycles else "n/a")
    print(f"Hedge cycles fired  : {n_hedges}")
    print()

    print("Legs filled per cycle:")
    leg_dist = Counter(legs_per_cycle)
    total = sum(leg_dist.values()) or 1
    for k in sorted(leg_dist):
        n = leg_dist[k]
        print(f"  {k} leg(s): {n:3d}  ({n/total*100:5.1f}%)")
    single = leg_dist.get(1, 0)
    print(f"\n  SINGLE-LEG RATE: {single/total*100:.1f}%  ({single}/{total})")
    print(f"  GRID ACTIVATED : {(total-single)/total*100:.1f}%  ({total-single}/{total})")
    print()

    print("Close reasons (classified):")
    rtotal = sum(reasons.values()) or 1
    for r, n in reasons.most_common():
        print(f"  {r:20s}: {n:3d}  ({n/rtotal*100:5.1f}%)")
    print()

    if durations:
        print("Cycle duration (seconds):")
        print(f"  median : {statistics.median(durations):.0f}")
        print(f"  mean   : {statistics.mean(durations):.0f}")
        print(f"  max    : {max(durations):.0f}")
        print(f"  min    : {min(durations):.0f}")
    print()

    if pnls:
        print(f"PnL (realized R) — {len(pnls)} closed cycles with pnl:")
        print(f"  sum R    : {sum(pnls):+.2f}")
        print(f"  mean R   : {statistics.mean(pnls):+.3f}")
        print(f"  median R : {statistics.median(pnls):+.3f}")
        wins = [r for r in pnls if r > 0]
        losses = [r for r in pnls if r <= 0]
        print(f"  wins     : {len(wins)} (avg +{statistics.mean(wins):.2f}R)" if wins else "  wins: 0")
        print(f"  losses   : {len(losses)} (avg {statistics.mean(losses):.2f}R)" if losses else "  losses: 0")
        wr = len(wins) / len(pnls) * 100
        print(f"  win rate : {wr:.1f}%")
    print()

    print("Per-symbol breakdown:")
    for sym in sorted(per_symbol_r):
        rs = per_symbol_r[sym]
        legs = per_symbol_legs[sym]
        single_pct = sum(1 for x in legs if x == 1) / len(legs) * 100 if legs else 0
        wr = sum(1 for r in rs if r > 0) / len(rs) * 100 if rs else 0
        print(
            f"  {sym}: n={len(rs):3d}  sum={sum(rs):+6.2f}R  "
            f"mean={statistics.mean(rs):+.2f}R  WR={wr:.0f}%  single-leg={single_pct:.0f}%"
        )


if __name__ == "__main__":
    main()
