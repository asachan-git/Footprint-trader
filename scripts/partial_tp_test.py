#!/usr/bin/env python3
"""Test partial profit-booking (scale-out) vs all-or-nothing close, on real M2 signals.

Current live behaviour: 100% of the cycle closes at one common TP (~2×ATR proxy).
This sims scale-out policies: book tranches at a TP ladder (in ATR mults) as price
travels, optionally move stop to break-even after the first tranche. SL = 5×ATR
(disaster floor). Walks each signal's real 1m path; SL-first intrabar (pessimistic),
matching walk_1m ordering. Realized R in disaster-floor units.

Compares each policy's avgR / WR / SL-rate to baseline on the SAME signals.
"""
from __future__ import annotations

import sys
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.m2_synthetic_backtest import FLOOR_PCT, atr, load_jsonl  # noqa: E402

MC_FILE = ROOT / "data" / "mode_compare.jsonl"
FP_DIR  = ROOT / "data" / "footprint"
SL_ATR  = 5.0


def walk_tranches(b1, signal_ts, entry, sl, side, tps, weights, move_be_after=None):
    """Walk 1m bars; book tranche `weights[i]` when price reaches `tps[i]` (in order).
    SL checked first each bar (pessimistic). Optional: after `move_be_after` tranches
    booked, raise stop to entry (BE). Returns realized price-move per unit risk-less:
    list of (weight, exit_price)."""
    window = [b for b in b1 if b["close_ts"] >= signal_ts]
    remaining = list(zip(weights, tps))   # unbooked tranches, nearest-first
    booked = []
    stop = sl
    n_booked = 0
    for bar in window:
        h, l = bar["ohlc"]["h"], bar["ohlc"]["l"]
        # SL / stop first
        hit_stop = (l <= stop) if side == "long" else (h >= stop)
        if hit_stop:
            for w, _ in remaining:
                booked.append((w, stop))
            return booked
        # book any reached TPs (in order)
        still = []
        for w, tp in remaining:
            reached = (h >= tp) if side == "long" else (l <= tp)
            if reached:
                booked.append((w, tp))
                n_booked += 1
                if move_be_after is not None and n_booked >= move_be_after:
                    stop = entry
            else:
                still.append((w, tp))
        remaining = still
        if not remaining:
            return booked
    # ran out of bars: close remaining at last close
    last_c = window[-1]["ohlc"]["c"] if window else entry
    for w, _ in remaining:
        booked.append((w, last_c))
    return booked


def policy_r(b1, ts, anchor, side, atr15, sym, tps_mult, weights, move_be_after=None):
    risk = anchor * FLOOR_PCT.get(sym, 0.02)
    if risk <= 0 or atr15 <= 0:
        return None
    sign = 1 if side == "long" else -1
    sl = anchor - sign * SL_ATR * atr15
    tps = [anchor + sign * m * atr15 for m in tps_mult]
    booked = walk_tranches(b1, ts, anchor, sl, side, tps, weights, move_be_after)
    r = 0.0
    for w, px in booked:
        r += w * (sign * (px - anchor)) / risk
    return r


POLICIES = {
    "baseline 100%@2ATR":      dict(tps_mult=[2.0],        weights=[1.0]),
    "50@1 / 50@2":             dict(tps_mult=[1.0, 2.0],   weights=[0.5, 0.5]),
    "50@1 / 50@2 +BE":         dict(tps_mult=[1.0, 2.0],   weights=[0.5, 0.5], move_be_after=1),
    "33@1 / 33@2 / 34@3":      dict(tps_mult=[1.0,2.0,3.0],weights=[0.33,0.33,0.34]),
    "33@1 / 33@2 / 34@3 +BE":  dict(tps_mult=[1.0,2.0,3.0],weights=[0.33,0.33,0.34], move_be_after=1),
    "50@0.5 / 50@2 +BE":       dict(tps_mult=[0.5, 2.0],   weights=[0.5, 0.5], move_be_after=1),
}


def stat(rs):
    if not rs:
        return "n=0"
    wr = sum(1 for r in rs if r > 0) / len(rs) * 100
    sl_rate = sum(1 for r in rs if r < -0.05) / len(rs) * 100
    return (f"n={len(rs):<4d} WR={wr:3.0f}% avgR={statistics.mean(rs):+.4f} "
            f"totR={sum(rs):+.2f} med={statistics.median(rs):+.4f} loss>5%={sl_rate:2.0f}%")


def main():
    sigs = [s for s in load_jsonl(MC_FILE)
            if s.get("side") in ("long", "short") and s.get("dry_run") and s.get("bar_id")]
    b15 = {s: sorted(load_jsonl(FP_DIR / f"{s}_15m.jsonl"), key=lambda b: b["close_ts"])
           for s in ("BTCUSDT", "XAUTUSDT")}
    b1  = {s: sorted(load_jsonl(FP_DIR / f"{s}_1m.jsonl"), key=lambda b: b["close_ts"])
           for s in ("BTCUSDT", "XAUTUSDT")}
    idx15 = {s: {b["close_ts"]: i for i, b in enumerate(b15[s])} for s in b15}

    # pre-resolve each signal's (sym, side, ts, anchor, atr)
    jobs = []
    for sig in sigs:
        sym, side = sig["symbol"], sig["side"]
        bts = int(sig["bar_id"].split("|")[2])
        i = idx15.get(sym, {}).get(bts)
        if i is None:
            continue
        bar = b15[sym][i]
        a = atr(b15[sym][max(0, i - 19):i + 1], 14)
        jobs.append((sym, side, bts, float(bar["ohlc"]["c"]), a))

    print("=" * 84)
    print(f"PARTIAL PROFIT-BOOKING TEST — real M2 signals  (n={len(jobs)})")
    print("=" * 84)
    results = {}
    for name, p in POLICIES.items():
        rs = []
        for sym, side, ts, anchor, a in jobs:
            r = policy_r(b1[sym], ts, anchor, side, a, sym, **p)
            if r is not None:
                rs.append(r)
        results[name] = rs
        print(f"{name:<26}: {stat(rs)}")

    base = statistics.mean(results["baseline 100%@2ATR"])
    print("\nΔ avgR vs baseline:")
    for name, rs in results.items():
        if name.startswith("baseline"):
            continue
        print(f"  {name:<26}: {statistics.mean(rs) - base:+.4f}")

    print("\nPer-symbol (best-looking policies):")
    for name in ["baseline 100%@2ATR", "50@1 / 50@2 +BE", "33@1 / 33@2 / 34@3 +BE"]:
        print(f"  [{name}]")
        for sym in b15:
            rs = []
            for s2, side, ts, anchor, a in jobs:
                if s2 != sym:
                    continue
                r = policy_r(b1[sym], ts, anchor, side, a, sym, **POLICIES[name])
                if r is not None:
                    rs.append(r)
            print(f"    {sym}: {stat(rs)}")


if __name__ == "__main__":
    main()
