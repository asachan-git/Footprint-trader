#!/usr/bin/env python3
"""Measure stacked-imbalance edge across footprint granularities + acceptance filter.

Why: footprint cells are built at price_step=0.1 for BOTH BTC and XAU. At that
granularity diagonal imbalance measures tick noise, not the $10-25 (BTC) / $1 (XAU)
stack a trader eyeballs. And the engine vote fired on stack *presence* alone, never
checking whether the candle *closed beyond* the zone (acceptance). This script
re-buckets the stored $0.1 ladders to coarser steps (exact aggregation, no re-ingest),
detects stacks at each step, and splits forward outcome by acceptance vs rejection.

Outcome per candidate continuation trade:
  buy  stack -> long  ; acceptance = close > zone.price_high
  sell stack -> short ; acceptance = close < zone.price_low
  entry = bar close ; SL=5xATR / TP=2xATR (simple_grid) ; walk 1m for first touch ;
  realized R via disaster floor (FLOOR_PCT).

Usage:
    python scripts/stacked_edge_scan.py --symbol BTCUSDT
    python scripts/stacked_edge_scan.py --symbol XAUTUSDT --source real
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.m2_synthetic_backtest import (  # noqa: E402
    FLOOR_PCT, atr, load_jsonl, simple_grid, walk_1m,
)
from pipeline.footprint import Cell, FootprintMatrix  # noqa: E402
from pipeline.features.stacked_imbalance import stacked_imbalances  # noqa: E402

FP_DIR = ROOT / "data" / "footprint"

# Fixed institutional cell width per symbol (matches vp_bin_size convention).
STEPS = {
    "BTCUSDT":  [25.0],
    "XAUTUSDT": [1.0],
}

MIN_STACK = 3
RATIO     = 3.0

# Regime: trailing N-bar return in ATR units. |slope| >= TREND_T → trending.
REGIME_N = 20
TREND_T  = 2.0


def regime_label(i: int, b15: list[dict], atr15: float) -> str:
    """Causal regime at bar i: trailing REGIME_N-bar return / ATR."""
    if i < REGIME_N or atr15 <= 0:
        return "warmup"
    c_now = float(b15[i]["ohlc"]["c"])
    c_past = float(b15[i - REGIME_N]["ohlc"]["c"])
    slope = (c_now - c_past) / atr15
    if slope >= TREND_T:
        return "trend_up"
    if slope <= -TREND_T:
        return "trend_down"
    return "range"


def rebucket(bar: dict, step: float) -> FootprintMatrix:
    """Re-aggregate a bar's $0.1 ladders into cells of width `step`.

    step<=0.1 (or 0) -> use raw stored prices unchanged (baseline).
    """
    bid: dict[float, float] = defaultdict(float)
    ask: dict[float, float] = defaultdict(float)

    def key(p: float) -> float:
        if step <= 0.1:
            return round(p, 6)
        return round(round(p / step) * step, 6)

    for lvl in bar.get("bid_ladder") or []:
        bid[key(lvl["price"])] += lvl["vol"]
    for lvl in bar.get("ask_ladder") or []:
        ask[key(lvl["price"])] += lvl["vol"]
    prices = sorted(set(bid) | set(ask))
    cells = tuple(Cell(price=p, bid_vol=bid.get(p, 0.0), ask_vol=ask.get(p, 0.0))
                  for p in prices)
    return FootprintMatrix(cells=cells)


def dominant_zone(zones) -> object | None:
    """Pick the stack zone most likely the one price interacts with: max count,
    tie-break widest span."""
    if not zones:
        return None
    return max(zones, key=lambda z: (z.count, z.price_high - z.price_low))


def outcome_r(bar: dict, bars_1m: list[dict], side: str, atr15: float,
              symbol: str) -> float | None:
    """Realized R of a continuation trade entered at this bar's close."""
    anchor = float(bar["ohlc"]["c"])
    entry, sl, tp = simple_grid(anchor, side, atr15, 3, symbol)
    if side == "long" and (tp <= entry or sl >= entry):
        return None
    if side == "short" and (tp >= entry or sl <= entry):
        return None
    risk = entry * FLOOR_PCT.get(symbol, 0.02)
    if risk <= 0:
        return None
    res = walk_1m(bars_1m, int(bar["close_ts"]), sl, tp, side)
    ep = res["exit_price"]
    return (ep - entry) / risk if side == "long" else (entry - ep) / risk


def fmt(rs: list[float]) -> str:
    if not rs:
        return f"{'n=0':>7}"
    wr = sum(1 for r in rs if r > 0) / len(rs) * 100
    avg = statistics.mean(rs)
    return f"n={len(rs):<4d} WR={wr:3.0f}% avgR={avg:+.4f} totR={sum(rs):+.2f}"


def fwd_mfe_mae(bar: dict, bars_1m: list[dict], side: str, atr15: float,
                k_bars: int = 15) -> tuple[float, float] | None:
    """Immediate-reaction probe: max favorable / adverse excursion (in ATR units)
    over the next k_bars 1m bars, measured in the stack/continuation direction."""
    if atr15 <= 0:
        return None
    anchor = float(bar["ohlc"]["c"])
    ts = int(bar["close_ts"])
    window = [b for b in bars_1m if b["close_ts"] > ts][:k_bars]
    if not window:
        return None
    hi = max(b["ohlc"]["h"] for b in window)
    lo = min(b["ohlc"]["l"] for b in window)
    if side == "long":
        mfe, mae = (hi - anchor), (anchor - lo)
    else:
        mfe, mae = (anchor - lo), (hi - anchor)
    return mfe / atr15, mae / atr15


def fmt_exc(xs: list[tuple[float, float]]) -> str:
    if not xs:
        return f"{'n=0':>7}"
    mfe = statistics.mean(x[0] for x in xs)
    mae = statistics.mean(x[1] for x in xs)
    edge = mfe - mae
    return f"n={len(xs):<4d} MFE={mfe:.2f} MAE={mae:.2f} edge={edge:+.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", "-s", required=True, choices=list(STEPS))
    ap.add_argument("--source", default="agg", choices=["agg", "real"],
                    help="agg = *_15m_agg.jsonl (~11d), real = *_15m.jsonl (~2.5d)")
    args = ap.parse_args()
    sym = args.symbol

    suffix = "_15m_agg" if args.source == "agg" else "_15m"
    b15 = load_jsonl(FP_DIR / f"{sym}{suffix}.jsonl")
    b1  = load_jsonl(FP_DIR / f"{sym}_1m.jsonl")
    b15.sort(key=lambda b: b["close_ts"])
    b1.sort(key=lambda b: b["close_ts"])
    if not b15 or not b1:
        print(f"[scan] missing data for {sym} (15m={len(b15)} 1m={len(b1)})")
        return

    print("=" * 78)
    print(f"STACKED-IMBALANCE EDGE SCAN — {sym}  source={args.source}  "
          f"min_stack={MIN_STACK} ratio={RATIO}")
    print(f"15m bars={len(b15)}  1m bars={len(b1)}")
    print("=" * 78)

    # ── noise floor: every bar, both directions, same SL/TP ──────────────────
    floor: list[float] = []
    for i, bar in enumerate(b15):
        prior = b15[:i + 1]
        a = atr(prior[-20:], 14) if len(prior) >= 2 else 1.0
        for side in ("long", "short"):
            r = outcome_r(bar, b1, side, a, sym)
            if r is not None:
                floor.append(r)
    print(f"\nNOISE FLOOR (all bars, long+short, same SL/TP):  {fmt(floor)}\n")

    REGIMES = ["trend_up", "range", "trend_down"]

    for step in STEPS[sym]:
        # regime -> side -> accepted/rejected lists (swing-R and MFE/MAE)
        R   = {rg: {sd: {"acc": [], "rej": []} for sd in ("long", "short")} for rg in REGIMES}
        X   = {rg: {sd: {"acc": [], "rej": []} for sd in ("long", "short")} for rg in REGIMES}
        # collapsed: trade class -> accepted/rejected
        CLASSES = ["with_trend", "counter_trend", "range"]
        CR = {cl: {"acc": [], "rej": []} for cl in CLASSES}
        CX = {cl: {"acc": [], "rej": []} for cl in CLASSES}
        regime_count = defaultdict(int)
        n_zone_bars = 0
        for i, bar in enumerate(b15):
            prior = b15[:i + 1]
            a = atr(prior[-20:], 14) if len(prior) >= 2 else 1.0
            rg = regime_label(i, b15, a)
            fp = rebucket(bar, step)
            zones = stacked_imbalances(fp, min_stack=MIN_STACK, ratio=RATIO)
            z = dominant_zone(zones)
            if z is None or rg not in REGIMES:
                continue
            n_zone_bars += 1
            regime_count[rg] += 1
            side = "long" if z.side == "buy" else "short"
            close = float(bar["ohlc"]["c"])
            accepted = (close > z.price_high) if side == "long" else (close < z.price_low)
            key = "acc" if accepted else "rej"
            # trade class relative to regime
            if rg == "range":
                cl = "range"
            elif (rg == "trend_up" and side == "long") or (rg == "trend_down" and side == "short"):
                cl = "with_trend"
            else:
                cl = "counter_trend"
            r = outcome_r(bar, b1, side, a, sym)
            x = fwd_mfe_mae(bar, b1, side, a)
            if r is not None:
                R[rg][side][key].append(r)
                CR[cl][key].append(r)
            if x is not None:
                X[rg][side][key].append(x)
                CX[cl][key].append(x)

        print(f"\nstep=${step:g}  zone-bars(non-warmup)={n_zone_bars}  "
              f"regime bars: " + " ".join(f"{rg}={regime_count[rg]}" for rg in REGIMES))
        header = f"{'regime':<11} | {'side':<5} | {'ACCEPTED (closed beyond)':<40} | REJECTED"
        print(header)
        print("-" * len(header))
        for rg in REGIMES:
            for side in ("long", "short"):
                a_r, r_r = R[rg][side]["acc"], R[rg][side]["rej"]
                a_x, r_x = X[rg][side]["acc"], X[rg][side]["rej"]
                if not (a_r or r_r):
                    continue
                print(f"{rg:<11} | {side:<5} | {fmt(a_r):<40} | {fmt(r_r)}")
                print(f"{'':<11} | {'~mfe':<5} | {fmt_exc(a_x):<40} | {fmt_exc(r_x)}")
            print("-" * len(header))

        # collapsed with-trend / counter-trend / range
        print(f"\nCOLLAPSED (with-trend / counter-trend / range)  step=${step:g}")
        ch = f"{'class':<14} | {'ACCEPTED (closed beyond)':<40} | REJECTED"
        print(ch)
        print("-" * len(ch))
        for cl in CLASSES:
            a_r, r_r = CR[cl]["acc"], CR[cl]["rej"]
            a_x, r_x = CX[cl]["acc"], CX[cl]["rej"]
            print(f"{cl:<14} | {fmt(a_r):<40} | {fmt(r_r)}")
            print(f"{'~mfe':<14} | {fmt_exc(a_x):<40} | {fmt_exc(r_x)}")
            print("-" * len(ch))


if __name__ == "__main__":
    main()
