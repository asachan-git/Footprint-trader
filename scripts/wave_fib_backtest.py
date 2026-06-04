#!/usr/bin/env python3
"""Backtest + sweep for the trend-continuation WaveFib (strategies/wave_fib.py).

Drives the LIVE arming path (WaveFib._arm_signal on bars[:i+1]) so the backtest can't
drift from paper: same continuation_leg structure detection, VP/Fib entry selection and
_seen dedup. On an armed LIMIT, simulate the pullback fill (≤ entry_expiry to touch,
else void) then first-touch SL/TP with a time-stop.

Sweeps the entry source (VP-poc / VP-value / Fib retrace) × TP measured-move (fib_ext)
× entry_expiry, ranked by PF. 15m only. Usage: .venv/bin/python scripts/wave_fib_backtest.py
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
from strategies.wave_fib import WaveFib

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TF = "15m"
MAX_HOLD = 48
WARMUP = 60
ARM_WITHIN = 8   # fixed (fractal pivots are fresh in-sample; swept separately if needed)

FIB_EXTS = [1.0, 1.382, 1.618, 2.0]
EXPIRYS = [4, 6, 9]
FIB_ENTRIES = [0.382, 0.5, 0.618]
# (entry_mode, vp_level) entry sources
VP_SOURCES = [("vp", "poc"), ("vp", "value")]


def _sim(side, level, sl, tp, future, expiry_bars):
    fill_k = next((k for k, b in enumerate(future[:expiry_bars])
                   if b.ohlc.l <= level <= b.ohlc.h), None)
    if fill_k is None:
        return None
    risk = abs(level - sl) or 1e-9
    for k, b in enumerate(future[fill_k + 1:]):
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
    return None


def run_variant(bars_by_sym, cfg_extra, expiry):
    trades, armed, filled = [], 0, 0
    for sym, bars in bars_by_sym.items():
        s = WaveFib(config={"symbols": [sym], "decide_tf": TF,
                            "arm_within": ARM_WITHIN, "entry_expiry_bars": expiry,
                            **cfg_extra})
        block_until = -1
        for i in range(WARMUP, len(bars) - 1):
            m = s._arm_signal(sym, bars[:i + 1])
            if m is None:
                continue
            armed += 1
            if i <= block_until:
                continue
            res = _sim(m["side"], float(m["entry"]), float(m["sl"]), float(m["tp"]),
                       bars[i + 1:], expiry)
            if res is None:
                continue
            filled += 1
            trades.append({"sym": sym, "side": m["side"], "r": res[0], "reason": res[1]})
            block_until = i + expiry + MAX_HOLD
    return trades, armed, filled


def summarize(trades):
    if not trades:
        return "n=0"
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    longs = sum(1 for t in trades if t["side"] == "long")
    g = sum(r for r in rs if r > 0)
    l = -sum(r for r in rs if r < 0)
    pf = g / l if l > 0 else float("inf")
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

    rows = []

    def sweep(label, cfg_extra):
        for fx, ex in product(FIB_EXTS, EXPIRYS):
            extra = {**cfg_extra, "fib_ext": fx}
            trades, armed, filled = run_variant(bars_by_sym, extra, ex)
            if len(trades) < 6:
                continue
            rows.append({"src": label, "fx": fx, "ex": ex, "n": len(trades),
                         "pf": _pf(trades), "sumR": sum(t["r"] for t in trades),
                         "wr": 100 * sum(1 for t in trades if t["r"] > 0) / len(trades),
                         "fill": 100 * filled / armed if armed else 0,
                         "trades": trades})

    for em, lv in VP_SOURCES:
        sweep(f"vp_{lv}", {"entry_mode": "vp", "vp_level": lv})
    for fe in FIB_ENTRIES:
        sweep(f"fib_{fe}", {"entry_mode": "fib", "fib_entry": fe})

    # ── ranked table ──
    rows.sort(key=lambda r: (r["pf"], r["sumR"]), reverse=True)
    print("=== Sweep: entry source × fib_ext (TP) × entry_expiry, ranked by PF (n≥6) ===")
    print(f"{'source':>10} {'fib_x':>6} {'exp':>4} {'n':>4} {'WR%':>5} "
          f"{'sumR':>7} {'PF':>5} {'fill%':>6}")
    for r in rows[:18]:
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"{r['src']:>10} {r['fx']:>6} {r['ex']:>4} {r['n']:>4} {r['wr']:>5.0f} "
              f"{r['sumR']:>+7.2f} {pf:>5} {r['fill']:>6.0f}")
    if not rows:
        print("  (no combo reached the 6-trade minimum — history too thin)")
        return

    # ── per-symbol split of the top combo ──
    best = rows[0]
    print(f"\n=== Top combo: {best['src']} fib_ext={best['fx']} exp={best['ex']} — per symbol ===")
    print(f"  ALL  {summarize(best['trades'])}")
    for s in SYMBOLS:
        sub = [t for t in best["trades"] if t["sym"] == s]
        if sub:
            print(f"  {s:9s} {summarize(sub)}")


if __name__ == "__main__":
    main()
