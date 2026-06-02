#!/usr/bin/env python3
"""Re-calibrate the direction engine against a TRADEABLE outcome (plan Part 2.2,
honest target) instead of raw N-bar close direction.

Reuses the cached votes in data/strategies/vote_dataset.jsonl (no re-compute) and
re-labels each bar by an ATR bracket simulated bar-by-bar on the real bars:
  long  bracket: entry=close, SL=close − SL_ATR×ATR, TP=close + TP_ATR×ATR
  short bracket: mirror.
A side "wins" if its TP is hit before its SL within K bars (SL-first on the same
bar = worst case). This respects the 2:1 R the strategies actually target.

CAVEAT: still a PROXY — democracy/republic exit via grid recovery + VP-anchored TP
+ no hard SL, not a fixed bracket. But a 2R bracket is far closer to the objective
than close-to-close direction. Treat as directional evidence, validate live.

Run: .venv/bin/python scripts/calibrate_trade.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging; logging.disable(logging.CRITICAL)

from pipeline.state_store import store

DATA = ROOT / "data" / "strategies" / "vote_dataset.jsonl"
SL_ATR, TP_ATR, K = 1.0, 2.0, 12


def _bracket_win(side, entry, a, fut):
    """True if TP hit before SL within K bars (SL-first same-bar). None if neither."""
    if a <= 0:
        return None
    if side == "long":
        sl, tp = entry - SL_ATR * a, entry + TP_ATR * a
        for b in fut[:K]:
            if b.ohlc.l <= sl:
                return False
            if b.ohlc.h >= tp:
                return True
    else:
        sl, tp = entry + SL_ATR * a, entry - TP_ATR * a
        for b in fut[:K]:
            if b.ohlc.h >= sl:
                return False
            if b.ohlc.l <= tp:
                return True
    return None


def main():
    rows = [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]
    # index bars by symbol/ts
    bars_by = {}
    idx_by = {}
    for sym in {r["symbol"] for r in rows}:
        bs = store().recent(sym, "15m", 10_000_000)
        bars_by[sym] = bs
        idx_by[sym] = {b.close_ts: i for i, b in enumerate(bs)}

    # attach bracket labels
    labeled = []
    for r in rows:
        bs = bars_by[r["symbol"]]
        i = idx_by[r["symbol"]].get(r["ts"])
        if i is None or i + 1 >= len(bs):
            continue
        fut = bs[i + 1:]
        a = r["atr"]
        lw = _bracket_win("long", r["close"], a, fut)
        sw = _bracket_win("short", r["close"], a, fut)
        r["long_win"], r["short_win"] = lw, sw
        labeled.append(r)

    res = [r for r in labeled if r["long_win"] is not None or r["short_win"] is not None]
    print(f"=== {len(labeled)} rows, {len(res)} with a resolved bracket "
          f"(SL {SL_ATR}×ATR / TP {TP_ATR}×ATR, {K} bars) ===")
    lwin = sum(1 for r in labeled if r["long_win"])
    swin = sum(1 for r in labeled if r["short_win"])
    print(f"long bracket win-rate  = {100*lwin/len(labeled):.1f}%  (need >33% to beat 2:1 RR breakeven)")
    print(f"short bracket win-rate = {100*swin/len(labeled):.1f}%")
    print("(2:1 bracket breakeven win-rate = 33.3%)\n")

    # engine side: does taking dd.side as a bracket win > breakeven? expected R?
    def er(winrate):
        return TP_ATR * winrate - SL_ATR * (1 - winrate)

    print("=== existing engine: side selection as a 2R bracket ===")
    n = win = 0
    by_bias = defaultdict(lambda: [0, 0])
    for r in labeled:
        if r["side"] not in ("long", "short"):
            continue
        w = r["long_win"] if r["side"] == "long" else r["short_win"]
        if w is None:
            continue
        n += 1; win += int(w)
        by_bias[r["bias"]][0] += 1; by_bias[r["bias"]][1] += int(w)
    if n:
        wr = win / n
        print(f"engine-side bracket win-rate = {100*wr:.1f}%  expectancy = {er(wr):+.3f}R  (n={n})")
        print(f"{'bias':>4s} {'n':>5s} {'win%':>6s} {'exp_R':>7s}")
        for b in sorted(by_bias):
            bn, bw = by_bias[b]
            w = bw / bn
            print(f"{b:>4d} {bn:>5d} {100*w:>5.1f}% {er(w):>+7.3f}")
        print("(want win% & exp_R to RISE with bias for it to be real confidence)\n")

    # per-vote: when a vote points long, does a long bracket win? (and short)
    print("=== per-vote: bracket win-rate when the vote points that way ===")
    agg = defaultdict(lambda: {"ln": 0, "lw": 0, "sn": 0, "sw": 0})
    for r in labeled:
        for mod, d in r.get("vote_dirs", {}).items():
            a = agg[mod]
            if d > 0 and r["long_win"] is not None:
                a["ln"] += 1; a["lw"] += int(r["long_win"])
            elif d < 0 and r["short_win"] is not None:
                a["sn"] += 1; a["sw"] += int(r["short_win"])
    print(f"{'module':22s} {'n':>5s} {'win%':>6s} {'exp_R':>7s}   (breakeven 33%)")
    for mod, a in sorted(agg.items(), key=lambda kv: -(kv[1]['ln'] + kv[1]['sn'])):
        nn = a["ln"] + a["sn"]; ww = a["lw"] + a["sw"]
        if nn == 0:
            continue
        w = ww / nn
        print(f"{mod:22s} {nn:>5d} {100*w:>5.1f}% {er(w):>+7.3f}")


if __name__ == "__main__":
    main()
