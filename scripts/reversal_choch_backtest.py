#!/usr/bin/env python3
"""Backtest + param sweep for the ChoCh→Fib reversal (strategies/reversal_choch.py).

Drives the LIVE arming code path: each bar i, feed bars[:i+1] to
ReversalChoch._arm_signal — identical detect_choch + impulse_leg + Fib math as paper,
including the _seen_choch one-trade-per-ChoCh dedup. When it arms a LIMIT, simulate the
retrace fill (≤ entry_expiry_bars to touch, else void) then first-touch SL/TP with a
time-stop. So the backtest cannot drift from live behaviour.

Full sweep over fib_entry × fib_ext × arm_within × entry_expiry_bars, ranked by PF.
Section 1 scores the three variants going live. 15m only — the ChoCh is a 15m signal.

Usage: .venv/bin/python scripts/reversal_choch_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from scripts.reversal_study import load_bars
from strategies.reversal_choch import ReversalChoch

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TF = "15m"
MAX_HOLD = 48          # bars before a time-stop (15m → 12h)
WARMUP = 60            # bars before the first possible arm (need swings + ATR)

# sweep grid
FIB_ENTRIES = [0.5, 0.618, 0.705]
FIB_EXTS = [1.272, 1.618, 2.0]
ARM_WITHINS = [5, 10, 15]
EXPIRYS = [4, 6, 9]

# the three live variants (name, fib_entry, fib_ext)
LIVE = [("A 0.618/1.618", 0.618, 1.618),
        ("B 0.705/1.272 OTE", 0.705, 1.272),
        ("C 0.5/2.0 deep", 0.5, 2.0)]


def _sim_fill_then_exit(side, level, sl, tp, future, expiry_bars):
    """Wait ≤ expiry_bars for price to touch the LIMIT `level`; on fill, first-touch
    SL/TP then time-stop. Returns (R, reason) or None (limit never filled)."""
    fill_k = None
    for k, b in enumerate(future[:expiry_bars]):
        if b.ohlc.l <= level <= b.ohlc.h:
            fill_k = k
            break
    if fill_k is None:
        return None
    risk = abs(level - sl) or 1e-9
    walk = future[fill_k + 1:]
    for k, b in enumerate(walk):
        if k >= MAX_HOLD:
            r = ((b.ohlc.c - level) if side == "long" else (level - b.ohlc.c)) / risk
            return r, "hold"
        if side == "long":
            if b.ohlc.l <= sl:
                return -abs(level - sl) / risk, "sl"
            if b.ohlc.h >= tp:
                return abs(tp - level) / risk, "tp"
        else:
            if b.ohlc.h >= sl:
                return -abs(sl - level) / risk, "sl"
            if b.ohlc.l <= tp:
                return abs(level - tp) / risk, "tp"
    return None  # ran out of history while open → drop (not counted)


def run_variant(bars_by_sym, fib_entry, fib_ext, arm_within, expiry):
    """Walk every symbol's history with one strategy instance; collect trade Rs."""
    trades = []
    armed = filled = 0
    for sym, bars in bars_by_sym.items():
        s = ReversalChoch(config={
            "symbols": [sym], "decide_tf": TF,
            "fib_entry": fib_entry, "fib_ext": fib_ext,
            "arm_within": arm_within, "entry_expiry_bars": expiry,
        })
        n = len(bars)
        block_until = -1
        for i in range(WARMUP, n - 1):
            m = s._arm_signal(sym, bars[:i + 1], TF)
            if m is None:
                continue
            armed += 1
            if i <= block_until:
                continue                      # don't overlap an open trade
            res = _sim_fill_then_exit(m["side"], float(m["entry"]), float(m["sl"]),
                                      float(m["tp"]), bars[i + 1:], expiry)
            if res is None:
                continue                      # limit voided (never touched)
            filled += 1
            trades.append({"sym": sym, "i": i, "side": m["side"],
                           "r": res[0], "reason": res[1]})
            block_until = i + expiry + MAX_HOLD
    return trades, armed, filled


def summarize(trades):
    if not trades:
        return "n=0"
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    longs = sum(1 for t in trades if t["side"] == "long")
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    pf = gains / losses if losses > 0 else float("inf")
    return (f"n={len(rs):3d}  WR={100*wins/len(rs):3.0f}%  sumR={sum(rs):+6.2f}  "
            f"avgR={statistics.mean(rs):+.3f}  PF={pf:4.2f}  L/S={longs}/{len(rs)-longs}")


def _pf(trades):
    g = sum(t["r"] for t in trades if t["r"] > 0)
    l = -sum(t["r"] for t in trades if t["r"] < 0)
    return g / l if l > 0 else (float("inf") if g > 0 else 0.0)


def main():
    bars_by_sym = {s: load_bars(s, TF) for s in SYMBOLS}
    for s, b in bars_by_sym.items():
        print(f"# {s} {TF}: {len(b)} bars")
    print()

    # ── 1. the three live variants (default arm_within=10, expiry=6) ──
    print("=== 1. Live variants (arm_within=10, entry_expiry=6) ===")
    for name, fe, fx in LIVE:
        trades, armed, filled = run_variant(bars_by_sym, fe, fx, 10, 6)
        fr = f"{100*filled/armed:.0f}%" if armed else "—"
        print(f"  {name:20s} fills={filled}/{armed} ({fr})  {summarize(trades)}")
        for s in SYMBOLS:
            sub = [t for t in trades if t["sym"] == s]
            if sub:
                print(f"      {s:9s} {summarize(sub)}")

    # ── 2. full sweep, ranked by PF (min 8 trades) ──
    print("\n=== 2. Full sweep: fib_entry × fib_ext × arm_within × entry_expiry ===")
    rows = []
    for fe, fx, aw, ex in product(FIB_ENTRIES, FIB_EXTS, ARM_WITHINS, EXPIRYS):
        trades, armed, filled = run_variant(bars_by_sym, fe, fx, aw, ex)
        if len(trades) < 8:
            continue
        rows.append({
            "fe": fe, "fx": fx, "aw": aw, "ex": ex, "n": len(trades),
            "pf": _pf(trades), "sumR": sum(t["r"] for t in trades),
            "wr": 100 * sum(1 for t in trades if t["r"] > 0) / len(trades),
            "fill": 100 * filled / armed if armed else 0,
        })
    rows.sort(key=lambda r: (r["pf"], r["sumR"]), reverse=True)
    print(f"{'fib_e':>6} {'fib_x':>6} {'arm':>4} {'exp':>4} {'n':>4} "
          f"{'WR%':>5} {'sumR':>7} {'PF':>5} {'fill%':>6}")
    for r in rows[:15]:
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"{r['fe']:>6} {r['fx']:>6} {r['aw']:>4} {r['ex']:>4} {r['n']:>4} "
              f"{r['wr']:>5.0f} {r['sumR']:>+7.2f} {pf:>5} {r['fill']:>6.0f}")
    if not rows:
        print("  (no combo reached the 8-trade minimum — history too thin)")

    # ── 3. best entry_expiry/arm_within per fib pair ──
    print("\n=== 3. Best (arm,exp) per fib pair (by PF, min 8) ===")
    for fe, fx in product(FIB_ENTRIES, FIB_EXTS):
        cand = [r for r in rows if r["fe"] == fe and r["fx"] == fx]
        if not cand:
            continue
        b = cand[0]
        pf = "inf" if b["pf"] == float("inf") else f"{b['pf']:.2f}"
        print(f"  {fe}/{fx}: arm={b['aw']} exp={b['ex']} n={b['n']} "
              f"WR={b['wr']:.0f}% sumR={b['sumR']:+.2f} PF={pf}")


if __name__ == "__main__":
    main()
