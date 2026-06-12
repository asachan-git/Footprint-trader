#!/usr/bin/env python3
"""Offline bracket backtest for reversal_eqhl + reversal_hvn (15m, BTC+XAUT).

Walks closed 15m bars, populating the sweep registry CAUSALLY each bar, asks the
strategy to .decide() at the cutoff (no future leak via store time-travel), then
simulates the trade forward with FIRST-TOUCH SL/TP (SL-first on the same bar,
time-stop at MAX_HOLD). One open trade at a time per (strategy, symbol) — mirrors
the manager's one-cycle-per-symbol gate.

APPROXIMATION — read these as bracket-R, not live-exact:
  - Models only the structural SL + VP-magnet TP bracket. It does NOT simulate the
    live cycle_manager exits (CVD-divergence cut, POC-trail tightening), which would
    close many trades earlier — so live avg-R/hold will differ.
  - TP uses the cached daily VP (vp_get) which is global, not time-travelled → mild
    lookahead on the TP level only.
  - Entry is market-at-signal-close (both strategies enter that way).

    .venv/bin/python -u scripts/reversal_fade_backtest.py
"""
from __future__ import annotations

import logging
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from pipeline.state_store import store
from pipeline.features.swing import build as swing_build
from pipeline.features import sweep as SW
from pipeline.features import cvd_div_state as cdv
from strategies.reversal_eqhl import ReversalEqHL
from strategies.reversal_hvn import ReversalHVN
from strategies.registry import build_enabled

TF = "15m"
SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
WARMUP = 150
MAX_HOLD = 32          # 15m bars → ~8h time-stop
WIN = 200              # rolling window for swing/sweep detection

S = store()
_orig = S.recent
CUT = {"ts": None}


def _recent(symbol, tf, n):
    bars = _orig(symbol, tf, 10_000_000)
    if CUT["ts"] is not None:
        bars = [b for b in bars if b.close_ts <= CUT["ts"]]
    return bars[-n:] if n else bars


S.recent = _recent


def _simulate(side, entry, sl, tp, future):
    """First-touch SL/TP, SL-first on a same-bar tie, time-stop at MAX_HOLD."""
    risk = abs(entry - sl) or 1e-9
    for k, b in enumerate(future):
        if k >= MAX_HOLD:
            r = ((b.ohlc.c - entry) if side == "long" else (entry - b.ohlc.c)) / risk
            return r, "hold", k
        if side == "long" and b.ohlc.l <= sl:   return -1.0, "sl", k
        if side == "short" and b.ohlc.h >= sl:  return -1.0, "sl", k
        if side == "long" and b.ohlc.h >= tp:   return abs(tp - entry) / risk, "tp", k
        if side == "short" and b.ohlc.l <= tp:  return abs(entry - tp) / risk, "tp", k
    if not future:
        return 0.0, "none", 0
    last = future[-1].ohlc.c
    r = ((last - entry) if side == "long" else (entry - last)) / risk
    return r, "hold", len(future) - 1


def backtest(strat, symbol):
    allb = _orig(symbol, TF, 10_000_000)
    if len(allb) < WARMUP + 10:
        return []
    SW._registry[symbol] = []          # causal registry, rebuilt bar-by-bar
    cdv._last.pop(symbol, None)         # don't leak persisted div state
    trades = []
    open_until = -1
    for i in range(WARMUP, len(allb) - 2):
        bar = allb[i]
        win = allb[max(0, i - WIN): i + 1]
        sp = swing_build(symbol, TF, win)
        SW.detect(bar, sp, prev_bars=allb[max(0, i - WIN): i])
        SW.tick_registry(symbol, bar)
        if i <= open_until:
            continue
        CUT["ts"] = bar.close_ts
        try:
            d = strat.decide(symbol, TF, bar, {})
        except Exception:
            continue
        if not d or d.side not in ("long", "short") or d.entry is None:
            continue
        entry = float(bar.ohlc.c)       # both strategies enter market-at-close
        sl = float(d.stop_loss); tp = float(d.take_profit)
        if abs(entry - sl) <= 0:
            continue
        r, reason, off = _simulate(d.side, entry, sl, tp, allb[i + 1:])
        trades.append({"side": d.side, "ts": bar.close_ts, "r": r, "reason": reason,
                       "rr": abs(tp - entry) / abs(entry - sl)})
        open_until = i + 1 + off
    return trades


def summ(trades):
    rs = [t["r"] for t in trades]
    if not rs:
        return "n=0"
    n = len(rs); wins = [r for r in rs if r > 0]; losses = [r for r in rs if r <= 0]
    wr = 100 * len(wins) / n
    gp = sum(wins); gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    reasons = Counter(t["reason"] for t in trades)
    return (f"n={n:<3} WR={wr:>3.0f}%  sumR={sum(rs):>7.2f}  avgR={statistics.mean(rs):>6.2f}  "
            f"PF={pf:>4.2f}  medRR={statistics.median(t['rr'] for t in trades):>4.1f}  {dict(reasons)}")


def main():
    # use the deployed configs (so gates/per-symbol match production)
    enabled = {s.name: s for s in build_enabled()}
    cfg_eqhl = enabled["reversal_eqhl"].config if "reversal_eqhl" in enabled else {}
    cfg_hvn = enabled["reversal_hvn"].config if "reversal_hvn" in enabled else {}
    specs = [("reversal_eqhl", ReversalEqHL, cfg_eqhl), ("reversal_hvn", ReversalHVN, cfg_hvn)]
    print(f"Bracket backtest (15m, first-touch SL/TP, SL-first, {MAX_HOLD}-bar time-stop)\n")
    allr = {}
    for name, cls, cfg in specs:
        print(f"=== {name} ===")
        combined = []
        for sym in SYMBOLS:
            CUT["ts"] = None
            tr = backtest(cls(config=cfg), sym)
            combined += tr
            print(f"  {sym:<9} {summ(tr)}")
        print(f"  {'COMBINED':<9} {summ(combined)}\n")
        allr[name] = combined
    return allr


if __name__ == "__main__":
    main()
