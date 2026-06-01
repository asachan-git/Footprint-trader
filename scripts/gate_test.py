#!/usr/bin/env python3
"""Test the adverse-regime gate on real M2 signals (mode_compare.jsonl).

For each fired M2 dry-run signal: classify regime at its bar (same _trend_regime
logic as direction_engine), label counter-trend, compute realized R (simple_grid +
walk_1m). Compare KEPT (with-trend / range) vs VETOED (counter-trend) and the net
effect of removing vetoed trades.
"""
from __future__ import annotations

import sys
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.m2_synthetic_backtest import (  # noqa: E402
    FLOOR_PCT, atr, load_jsonl, simple_grid, walk_1m,
)

MC_FILE = ROOT / "data" / "mode_compare.jsonl"
FP_DIR  = ROOT / "data" / "footprint"
REGIME_N, TREND_T = 20, 2.0


def regime_at(b15: list[dict], idx: int) -> str:
    if idx < REGIME_N:
        return "range"
    a = atr(b15[max(0, idx - 19):idx + 1], 14)
    if a <= 0:
        return "range"
    slope = (b15[idx]["ohlc"]["c"] - b15[idx - REGIME_N]["ohlc"]["c"]) / a
    if slope >= TREND_T:
        return "trend_up"
    if slope <= -TREND_T:
        return "trend_down"
    return "range"


def realized_r(bar: dict, b1: list[dict], side: str, atr15: float, sym: str):
    anchor = float(bar["ohlc"]["c"])
    entry, sl, tp = simple_grid(anchor, side, atr15, 3, sym)
    if side == "long" and (tp <= entry or sl >= entry):
        return None
    if side == "short" and (tp >= entry or sl <= entry):
        return None
    risk = entry * FLOOR_PCT.get(sym, 0.02)
    if risk <= 0:
        return None
    ep = walk_1m(b1, int(bar["close_ts"]), sl, tp, side)["exit_price"]
    return (ep - entry) / risk if side == "long" else (entry - ep) / risk


def stat(rs):
    if not rs:
        return "n=0"
    wr = sum(1 for r in rs if r > 0) / len(rs) * 100
    return f"n={len(rs):<4d} WR={wr:3.0f}% avgR={statistics.mean(rs):+.4f} totR={sum(rs):+.3f}"


def main():
    sigs = [s for s in load_jsonl(MC_FILE)
            if s.get("side") in ("long", "short") and s.get("dry_run") and s.get("bar_id")]
    b15 = {s: [] for s in ("BTCUSDT", "XAUTUSDT")}
    b1  = {s: [] for s in ("BTCUSDT", "XAUTUSDT")}
    for s in b15:
        b15[s] = sorted(load_jsonl(FP_DIR / f"{s}_15m.jsonl"), key=lambda b: b["close_ts"])
        b1[s]  = sorted(load_jsonl(FP_DIR / f"{s}_1m.jsonl"), key=lambda b: b["close_ts"])
    idx15 = {s: {b["close_ts"]: i for i, b in enumerate(b15[s])} for s in b15}

    kept, vetoed = [], []
    per = {s: {"kept": [], "vetoed": []} for s in b15}
    n_skip = 0
    for sig in sigs:
        sym, side = sig["symbol"], sig["side"]
        bts = int(sig["bar_id"].split("|")[2])
        i = idx15.get(sym, {}).get(bts)
        if i is None:
            n_skip += 1
            continue
        rg = regime_at(b15[sym], i)
        counter = (rg == "trend_up" and side == "short") or (rg == "trend_down" and side == "long")
        a = atr(b15[sym][max(0, i - 19):i + 1], 14)
        r = realized_r(b15[sym][i], b1[sym], side, a, sym)
        if r is None:
            n_skip += 1
            continue
        (vetoed if counter else kept).append(r)
        per[sym]["vetoed" if counter else "kept"].append(r)

    allr = kept + vetoed
    print("=" * 64)
    print("ADVERSE-REGIME GATE TEST — real M2 signals (mode_compare.jsonl)")
    print("=" * 64)
    print(f"signals={len(sigs)}  tested={len(allr)}  skipped={n_skip}\n")
    print(f"ALL signals (no gate)     : {stat(allr)}")
    print(f"KEPT  (with-trend/range)  : {stat(kept)}")
    print(f"VETOED (counter-trend)    : {stat(vetoed)}")
    if allr and kept:
        d = statistics.mean(kept) - statistics.mean(allr)
        print(f"\nGate effect: removes {len(vetoed)}/{len(allr)} trades "
              f"({len(vetoed)/len(allr)*100:.0f}%), avgR {statistics.mean(allr):+.4f} → "
              f"{statistics.mean(kept):+.4f} ({d:+.4f})")
    print()
    for s in b15:
        print(f"{s}: KEPT {stat(per[s]['kept'])}  ||  VETOED {stat(per[s]['vetoed'])}")


if __name__ == "__main__":
    main()
