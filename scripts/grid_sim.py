#!/usr/bin/env python3
"""Grid-aware cycle simulator — models the actual multi-leg averaging grid, so the
SURVIVAL benefit of partial profit-booking (margin/inventory relief, banking a bounce
before a cycle dies) becomes measurable instead of assumed.

Faithful to live mechanics (grid_placer.py / cycle_manager.py), simplified where live
needs VP-cache state:
  legs    : Fib [1,1,2,3,5] lots; leg1=anchor (immediate), legs 2..n at i×step×ATR
            on the adverse side (step_mult 0.5). FAR legs carry MORE lots (averaging).
  avg     : recomputed on each fill, lot-weighted.
  TP      : leg1 ± tp_mult×ATR (range 2.0). Close ALL when price reaches TP and avg in profit.
  SL      : leg_last ∓ max(5×ATR, floor_pct×anchor)  (disaster floor; BTC 3% / XAU 1.5%).
  escape  : all legs filled AND price stays ESCAPE_ATR beyond last leg for ESCAPE_BARS
            1m bars → force-close (runaway-trend death).
Realized R in disaster-floor units: Σ lots_i·(exit−entry_i)·sign / (Σ lots_i · anchor · floor_pct).

Partial policy: once price recovers PROFIT_BUF×ATR beyond avg with ≥2 legs filled, close
a fraction of open lots (bank the bounce, cut inventory); remainder rides to TP.

Compares baseline (hold-all) vs partial on expectancy AND survival.
"""
from __future__ import annotations

import sys
import statistics
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.m2_synthetic_backtest import atr, load_jsonl  # noqa: E402

MC_FILE = ROOT / "data" / "mode_compare.jsonl"
FP_DIR  = ROOT / "data" / "footprint"

FIB        = [1, 1, 2, 3, 5]
STEP_MULT  = 0.5
TP_MULT    = 2.0
SL_MULT    = 5.0
FLOOR_PCT  = {"BTCUSDT": 0.03, "XAUTUSDT": 0.015}
ESCAPE_ATR = 0.5
ESCAPE_BARS = 45          # 3×15m of no recovery beyond last leg
PROFIT_BUF = 0.5          # partial trigger: price this many ATR beyond avg


@dataclass
class CycleResult:
    outcome: str            # tp | sl | escape | timeout
    realized_r: float
    peak_lots: float        # max simultaneous open lots (inventory / margin proxy)
    peak_dd_r: float        # worst unrealized drawdown in R
    bars_held: int


def _legs(anchor: float, side: str, atr15: float, n: int):
    sign = -1 if side == "long" else 1     # adverse direction
    out = []
    for i in range(n):
        price = anchor + sign * (i * STEP_MULT * atr15)
        out.append((price, float(FIB[i])))
    return out                              # [(price, lots)] nearest→far


def simulate(b1, ts, anchor, side, atr15, sym, n_legs=5, partial_frac=0.0,
             mode="off", keep_near=2):
    """Walk 1m bars; return CycleResult.
    mode='conservative': close partial_frac of every filled leg on first bounce.
    mode='aggressive'  : on first bounce, dump ALL filled legs except nearest `keep_near`
                         (the deep large-lot legs, bought cheap → profit), then move the
                         remaining legs' stop to break-even (remaining avg)."""
    fp = FLOOR_PCT.get(sym, 0.02)
    if atr15 <= 0 or anchor <= 0:
        return None
    sign = 1 if side == "long" else -1
    legs = _legs(anchor, side, atr15, n_legs)
    leg_last_price = legs[-1][0]
    risk_unit = anchor * fp
    sl_off = max(SL_MULT * atr15, anchor * fp)
    sl = leg_last_price - sign * sl_off
    tp = anchor + sign * TP_MULT * atr15

    filled = [(anchor, legs[0][1])]         # leg1 fills immediately
    next_leg = 1
    booked_r = 0.0                          # realized R from partial closes
    peak_lots = legs[0][1]
    peak_dd_r = 0.0
    partial_done = False
    be_active = False       # aggressive: remaining legs stopped at break-even
    bars_against = 0

    window = [b for b in b1 if b["close_ts"] >= ts]

    def avg_and_lots():
        L = sum(l for _, l in filled)
        a = sum(p * l for p, l in filled) / L if L else anchor
        return a, L

    # booked_r accumulates partial-close PnL in lots×price units; final R divides by
    # (total lots over the whole cycle × risk_unit) so partial and held legs share one base.
    booked_lots_total = 0.0

    for n_bar, bar in enumerate(window):
        h, l, c = bar["ohlc"]["h"], bar["ohlc"]["l"], bar["ohlc"]["c"]

        # 1) fill deeper legs as price moves adverse
        while next_leg < n_legs:
            lp, lots = legs[next_leg]
            reached = (l <= lp) if side == "long" else (h >= lp)
            if not reached:
                break
            filled.append((lp, lots))
            next_leg += 1
        cur_lots = sum(l for _, l in filled)
        peak_lots = max(peak_lots, cur_lots)

        a, L = avg_and_lots()

        # 2) stop hit — disaster SL, or break-even stop after aggressive dump
        if (l <= sl) if side == "long" else (h >= sl):
            tot = L + booked_lots_total
            r = (booked_r + sum(lt * sign * (sl - p) for p, lt in filled)) / (tot * risk_unit)
            return CycleResult("be" if be_active else "sl", r, peak_lots, peak_dd_r, n_bar + 1)

        # 3) scale-out on first bounce (price recovered PROFIT_BUF×ATR beyond avg)
        in_profit = (c > a + PROFIT_BUF * atr15) if side == "long" else (c < a - PROFIT_BUF * atr15)
        if not partial_done and in_profit and len(filled) >= 2:
            px = c
            if mode == "conservative" and partial_frac > 0:
                booked_r += sum((lt * partial_frac) * sign * (px - p) for p, lt in filled)
                booked_lots_total += sum(lt * partial_frac for _, lt in filled)
                filled = [(p, lt * (1 - partial_frac)) for p, lt in filled]
                partial_done = True
            elif mode == "aggressive" and len(filled) > keep_near:
                # dump the deep large-lot legs entirely (bought cheap → now in profit),
                # keep nearest `keep_near`, move their stop to break-even
                dump = filled[keep_near:]
                booked_r += sum(lt * sign * (px - p) for p, lt in dump)
                booked_lots_total += sum(lt for _, lt in dump)
                filled = filled[:keep_near]
                be_avg, _ = avg_and_lots()
                sl = be_avg
                be_active = True
                partial_done = True

        # 4) TP — all (remaining) legs close when price reaches tp and avg in profit
        tp_profit = (tp > a) if side == "long" else (tp < a)
        tp_hit = (h >= tp) if side == "long" else (l <= tp)
        if tp_profit and tp_hit:
            tot = L + booked_lots_total
            r = (booked_r + sum(lt * sign * (tp - p) for p, lt in filled)) / (tot * risk_unit)
            return CycleResult("tp", r, peak_lots, peak_dd_r, n_bar + 1)

        # 5) unrealized DD track
        unreal = sum(lt * sign * (c - p) for p, lt in filled) / ((L + booked_lots_total) * risk_unit)
        peak_dd_r = min(peak_dd_r, unreal)

        # 6) trend-escape: all legs filled, price beyond last leg, no recovery
        beyond = (c < leg_last_price - ESCAPE_ATR * atr15) if side == "long" \
            else (c > leg_last_price + ESCAPE_ATR * atr15)
        if next_leg >= n_legs and beyond:
            bars_against += 1
            if bars_against >= ESCAPE_BARS:
                tot = L + booked_lots_total
                r = (booked_r + sum(lt * sign * (c - p) for p, lt in filled)) / (tot * risk_unit)
                return CycleResult("escape", r, peak_lots, peak_dd_r, n_bar + 1)
        else:
            bars_against = 0

    # timeout: close remaining at last close
    last_c = window[-1]["ohlc"]["c"] if window else anchor
    a, L = avg_and_lots()
    tot = L + booked_lots_total
    r = (booked_r + sum(lt * sign * (last_c - p) for p, lt in filled)) / (tot * risk_unit) if tot else 0.0
    return CycleResult("timeout", r, peak_lots, peak_dd_r, len(window))


def summarize(name, results):
    rs = [c.realized_r for c in results]
    wr = sum(1 for r in rs if r > 0) / len(rs) * 100
    outc = {}
    for c in results:
        outc[c.outcome] = outc.get(c.outcome, 0) + 1
    loss_tail = [r for r in rs if r < 0]
    avg_loss = statistics.mean(loss_tail) if loss_tail else 0.0
    print(f"{name:<22}: n={len(rs):<4d} WR={wr:3.0f}% avgR={statistics.mean(rs):+.4f} "
          f"totR={sum(rs):+.2f} avgLoss={avg_loss:+.3f}")
    print(f"{'':<22}  peakLots={statistics.mean(c.peak_lots for c in results):.2f} "
          f"peakDD_R={statistics.mean(c.peak_dd_r for c in results):+.3f} "
          f"bars={statistics.mean(c.bars_held for c in results):.0f} "
          f"| {dict(sorted(outc.items()))}")


def main():
    sigs = [s for s in load_jsonl(MC_FILE)
            if s.get("side") in ("long", "short") and s.get("dry_run") and s.get("bar_id")]
    b15 = {s: sorted(load_jsonl(FP_DIR / f"{s}_15m.jsonl"), key=lambda b: b["close_ts"])
           for s in ("BTCUSDT", "XAUTUSDT")}
    b1  = {s: sorted(load_jsonl(FP_DIR / f"{s}_1m.jsonl"), key=lambda b: b["close_ts"])
           for s in ("BTCUSDT", "XAUTUSDT")}
    idx15 = {s: {b["close_ts"]: i for i, b in enumerate(b15[s])} for s in b15}

    jobs = []
    for sig in sigs:
        sym, side = sig["symbol"], sig["side"]
        bts = int(sig["bar_id"].split("|")[2])
        i = idx15.get(sym, {}).get(bts)
        if i is None:
            continue
        a = atr(b15[sym][max(0, i - 19):i + 1], 14)
        jobs.append((sym, side, bts, float(b15[sym][i]["ohlc"]["c"]), a))

    print("=" * 86)
    print(f"GRID-AWARE CYCLE SIM — real M2 signals (n={len(jobs)})  "
          f"step={STEP_MULT}ATR tp={TP_MULT}ATR sl={SL_MULT}ATR")
    print("=" * 86)

    configs = [
        ("baseline (hold all)", dict(mode="off")),
        ("conservative 33%",    dict(mode="conservative", partial_frac=0.33)),
        ("conservative 50%",    dict(mode="conservative", partial_frac=0.50)),
        ("aggressive keep1",    dict(mode="aggressive", keep_near=1)),
        ("aggressive keep2",    dict(mode="aggressive", keep_near=2)),
    ]
    for label, kw in configs:
        res = [simulate(b1[s], ts, anc, side, a, s, **kw) for s, side, ts, anc, a in jobs]
        res = [r for r in res if r is not None]
        summarize(label, res)
        print()


if __name__ == "__main__":
    main()
