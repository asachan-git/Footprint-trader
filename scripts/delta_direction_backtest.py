#!/usr/bin/env python3
"""Backtest the two DELTA-DIRECTION setups before building strategies (15m, BTC+XAUT).

  Setup 1 — ANCHOR ZONE DEFEND (fade re-entry, trade WITH the anchor's delta):
    A high-delta anchor bar marks a zone. When price re-enters it and the bar
    defends in the anchor's delta direction (anchor_bar.test_retest=="continuation"),
    trade that direction (bearish anchor → short, bullish → long). SL beyond the
    defended extreme; TP 2R bracket.

  Setup 2 — EqHL SWEEP CONTINUATION (trade WITH the breakout delta):
    On a fresh EqH/EqL sweep, if the sweep bar's net delta agrees with the breakout
    (sweep_high & delta>0 → long; sweep_low & delta<0 → short), continue that way.
    SL beyond the sweep's opposite extreme; TP 2R bracket.

Reuses the reversal_fade_backtest harness (store time-travel, first-touch SL/TP,
SL-first, time-stop, one-trade-at-a-time). Bracket-only — no live exits modelled.

    .venv/bin/python -u scripts/delta_direction_backtest.py
"""
from __future__ import annotations

import logging, statistics, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

import scripts.reversal_fade_backtest as bt   # reuse CUT, _recent, _simulate, _orig
from pipeline.features import anchor_bar as AB
from pipeline.features import sweep as SW
from pipeline.features.swing import build as swing_build
from pipeline.features.atr import atr

TF = "15m"; SYMBOLS = ["BTCUSDT", "XAUTUSDT"]; WARMUP = 150; WIN = 200; RR = 2.0
S = bt.S; _orig = bt._orig
REGIME_N = 20; TREND_T = 2.0   # matches direction_engine._trend_regime


def regime_at(allb, i):
    """Causal trend regime from trailing REGIME_N bars: slope in ATR units."""
    if i < REGIME_N + 1:
        return "range", 0.0
    a = atr(allb[i - REGIME_N: i + 1]) or 0.0
    if a <= 0:
        return "range", 0.0
    slope = (allb[i].ohlc.c - allb[i - REGIME_N].ohlc.c) / a
    if slope >= TREND_T:
        return "trend_up", slope
    if slope <= -TREND_T:
        return "trend_down", slope
    return "range", slope


def _tag(side, allb, i):
    reg, slope = regime_at(allb, i)
    with_trend = (side == "long" and reg == "trend_up") or (side == "short" and reg == "trend_down")
    counter = (side == "long" and reg == "trend_down") or (side == "short" and reg == "trend_up")
    bucket = "with_trend" if with_trend else ("counter_trend" if counter else "range")
    return {"regime": reg, "bucket": bucket}


def _summ(trades):
    rs = [t["r"] for t in trades]
    if not rs:
        return "n=0"
    n = len(rs); w = [r for r in rs if r > 0]; l = [r for r in rs if r <= 0]
    pf = (sum(w) / -sum(l)) if (l and sum(l) < 0) else float("inf")
    return (f"n={n:<3} WR={100*len(w)/n:>3.0f}%  avgR={statistics.mean(rs):>6.2f}  "
            f"sumR={sum(rs):>7.1f}  PF={pf:>4.2f}  {dict(Counter(t['reason'] for t in trades))}")


def bt_anchor(symbol):
    allb = _orig(symbol, TF, 10_000_000)
    AB._sessions[symbol] = []
    trades = []; open_until = -1; acted = set()
    for i in range(WARMUP, len(allb) - 2):
        bar = allb[i]
        recent60 = allb[max(0, i - 60): i + 1]
        AB.update(symbol, bar, recent60)                 # detect/register/tick (causal)
        if i <= open_until:
            continue
        a = atr(allb[max(0, i - 14): i + 1]) or 0.0
        if a <= 0:
            continue
        for anchor in AB.active_anchors(symbol, bar.ohlc.c, a):
            rr = AB.test_retest(anchor, bar)
            if rr.pattern != "continuation":
                continue
            key = anchor.bar_id
            if key in acted:
                continue
            side = "long" if anchor.delta_sign == "bull" else "short"
            entry = float(bar.ohlc.c)
            buf = 0.10 * a
            sl = (anchor.high + buf) if side == "short" else (anchor.low - buf)
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry + RR * risk if side == "long" else entry - RR * risk
            r, reason, off = bt._simulate(side, entry, sl, tp, allb[i + 1:])
            trades.append({"side": side, "r": r, "reason": reason, **_tag(side, allb, i)})
            acted.add(key); open_until = i + 1 + off
            break
    return trades


def bt_eqhl_cont(symbol):
    allb = _orig(symbol, TF, 10_000_000)
    SW._registry[symbol] = []
    trades = []; open_until = -1; acted = set()
    for i in range(WARMUP, len(allb) - 2):
        bar = allb[i]; win = allb[max(0, i - WIN): i + 1]
        sp = swing_build(symbol, TF, win)
        SW.detect(bar, sp, prev_bars=allb[max(0, i - WIN): i]); SW.tick_registry(symbol, bar)
        if i <= open_until:
            continue
        a = atr(allb[max(0, i - 14): i + 1]) or 0.0
        if a <= 0:
            continue
        d = bar.delta or 0.0
        for sw in SW.active_sweeps(symbol):
            if sw.level_label not in ("equal_high", "equal_low") or sw.stale or sw.age_bars > 2:
                continue
            # continuation: breakout delta agrees with the sweep direction
            if sw.sweep_type == "sweep_high" and d > 0:
                side = "long"; sl = min(bar.ohlc.l, sw.swept_level) - 0.10 * a
            elif sw.sweep_type == "sweep_low" and d < 0:
                side = "short"; sl = max(bar.ohlc.h, sw.swept_level) + 0.10 * a
            else:
                continue
            key = f"{sw.sweep_type}|{round(sw.swept_level,2)}"
            if key in acted:
                continue
            entry = float(bar.ohlc.c); risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry + RR * risk if side == "long" else entry - RR * risk
            r, reason, off = bt._simulate(side, entry, sl, tp, allb[i + 1:])
            trades.append({"side": side, "r": r, "reason": reason, **_tag(side, allb, i)})
            acted.add(key); open_until = i + 1 + off
            break
    return trades


def main():
    print(f"Delta-direction backtest (15m, 2R bracket, first-touch, {bt.MAX_HOLD}-bar stop)\n")
    for label, fn in [("ANCHOR-zone defend (with delta)", bt_anchor),
                      ("EqHL sweep CONTINUATION (with delta)", bt_eqhl_cont)]:
        print(f"=== {label} ===")
        comb = []
        for sym in SYMBOLS:
            bt.CUT["ts"] = None
            tr = fn(sym); comb += tr
            print(f"  {sym:<9} {_summ(tr)}")
        print(f"  {'COMBINED':<9} {_summ(comb)}\n")


if __name__ == "__main__":
    main()
