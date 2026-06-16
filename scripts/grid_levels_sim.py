#!/usr/bin/env python3
"""Backtest harness for the footprint-driven neutral-grid planner.

Replays stored bars through execution.grid_planner.plan_grid_levels() and labels
each ARM plan by walking forward: did a BUY-side leg fill and reach buy_tp, or a
SELL-side leg fill and reach sell_tp, BEFORE the opposite side's outermost leg got
run over to a loss? Reports neutral-straddle edge per trigger kind.

This measures the PLACEMENT edge only (no EA, no broker, no margin). Causal note:
the planner sees only bars up to the decision bar (series truncated per step);
labeling uses future bars (correct — outcomes are always future).

    .venv/bin/python scripts/grid_levels_sim.py --symbol BTCUSDT --tf 5m --max 2000

Per-bar planning over a long series is expensive (VP/anchor recompute); use
--stride to sample every Nth bar.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging
logging.disable(logging.CRITICAL)

import pipeline.state_store as ss
from execution.grid_planner import plan_grid_levels


def _label(plan, future_bars, ref_price: float = 0.0):
    """Simulate the straddle's real P&L lifecycle over future_bars.

    `ref_price` = market price at placement (the decision-bar close). Pending STOP
    orders are only valid on their correct side of it: a BuyStop must sit ABOVE the
    market, a SellStop BELOW. Legs on the wrong side cannot be placed as stops (MT5
    rejects them), so they are dropped — this prevents the fake instant-fill wins
    that a stale/far fulcrum produced (e.g. a 66k "buy leg" filling while price is
    80k, then tagging a 66.4k "TP").

    Models the EA's actual close semantics:
      - Each leg is a stop order: a BuyStop fills when High >= its price; a
        SellStop fills when Low <= its price.
      - BuyStops carry buy_tp, SellStops carry sell_tp (one shared TP per side).
      - Grid closes the WHOLE cycle when price touches either TP (the EA's
        gBuyTP/gSellTP watch) → realised P&L of all FILLED legs at that price.
      - FULL HEDGE: when every buy AND every sell leg has filled, the position
        is delta-neutral and the EA force-closes at the floating loss.
      - Lookahead expiry → mark-to-market at last close (outcome 'open').

    P&L per leg in PRICE units × lot (BTC/XAU $ per unit). Buy leg pnl =
    (exit - entry)*lot; sell leg pnl = (entry - exit)*lot. Returns
    (outcome, pnl_units) where outcome ∈ {'win','loss','open'} and pnl_units is
    realised price-points×lot (sign = profit/loss).
    """
    # Keep only legs that are VALID stop orders relative to the placement price:
    # BuyStops above the market, SellStops below. Wrong-side legs are unplaceable.
    if ref_price > 0:
        buys = sorted((l for l in plan.buy_legs if l.price > ref_price), key=lambda l: l.price)
        sells = sorted((l for l in plan.sell_legs if l.price < ref_price), key=lambda l: l.price, reverse=True)
    else:
        buys = sorted(plan.buy_legs, key=lambda l: l.price)      # nearest-mid first
        sells = sorted(plan.sell_legs, key=lambda l: l.price, reverse=True)
    n_buy, n_sell = len(buys), len(sells)
    if n_buy == 0 and n_sell == 0:
        return "open", 0.0
    buy_filled = [False] * n_buy
    sell_filled = [False] * n_sell

    def _pnl_at(exit_px):
        p = 0.0
        for l, f in zip(buys, buy_filled):
            if f:
                p += (exit_px - l.price) * l.lot
        for l, f in zip(sells, sell_filled):
            if f:
                p += (l.price - exit_px) * l.lot
        return p

    for b in future_bars:
        h, lo, c = b.ohlc.h, b.ohlc.l, b.ohlc.c
        # 1) TP / full-hedge resolve FIRST, against fills accumulated on PRIOR bars
        #    only. A leg that fills this bar cannot also realise its TP this same bar
        #    (a single candle reaching both the stop and the TP is path-ambiguous and
        #    was the source of fake instant-fill wins for at-price fulcrums).
        buy_tp_hit = bool(plan.buy_tp) and h >= plan.buy_tp and any(buy_filled)
        sell_tp_hit = bool(plan.sell_tp) and lo <= plan.sell_tp and any(sell_filled)
        if buy_tp_hit and sell_tp_hit:
            # both targets printed in one bar = whipsaw, intrabar path unknown →
            # pessimistic: take the worse side.
            pnl = min(_pnl_at(plan.buy_tp), _pnl_at(plan.sell_tp))
            return ("win" if pnl > 0 else "loss"), pnl
        if buy_tp_hit:
            pnl = _pnl_at(plan.buy_tp)
            return ("win" if pnl > 0 else "loss"), pnl
        if sell_tp_hit:
            pnl = _pnl_at(plan.sell_tp)
            return ("win" if pnl > 0 else "loss"), pnl
        if all(buy_filled) and all(sell_filled) and n_buy and n_sell:
            pnl = _pnl_at(c)
            return "loss", pnl
        # 2) NOW apply this bar's fills (TP-eligible only from the next bar onward)
        for i, l in enumerate(buys):
            if not buy_filled[i] and h >= l.price:
                buy_filled[i] = True
        for i, l in enumerate(sells):
            if not sell_filled[i] and lo <= l.price:
                sell_filled[i] = True
        # full hedge that COMPLETES on this bar's fills → force close at the close
        if all(buy_filled) and all(sell_filled) and n_buy and n_sell:
            pnl = _pnl_at(c)
            return "loss", pnl
    # 4) lookahead expiry → mark-to-market
    last_c = future_bars[-1].ohlc.c if future_bars else 0.0
    return "open", _pnl_at(last_c)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--max", type=int, default=2000, help="max decision bars to replay (from the end)")
    ap.add_argument("--stride", type=int, default=1, help="sample every Nth bar")
    ap.add_argument("--lookahead", type=int, default=40, help="bars forward for labeling")
    ap.add_argument("--warmup", type=int, default=120, help="min history before first decision")
    ap.add_argument("--trigger-hint", default="", help="restrict the planner to one trigger kind")
    args = ap.parse_args()

    st = ss.store()
    series = list(st._bars.get((args.symbol, args.tf), []))
    if len(series) < args.warmup + args.lookahead + 10:
        print(f"not enough bars for {args.symbol} {args.tf}: have {len(series)}")
        return

    key = (args.symbol, args.tf)
    n = len(series)
    start = max(args.warmup, n - args.max)
    stop = n - args.lookahead

    stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"arm": 0, "win": 0, "loss": 0, "open": 0, "pnl": 0.0})
    skips: dict[str, int] = defaultdict(int)
    armed = 0

    for i in range(start, stop, args.stride):
        # Causal window: planner sees only bars [0..i].
        st._bars[key] = series[: i + 1]
        decision_bar = series[i]
        try:
            plan = plan_grid_levels(args.symbol, args.tf, decision_bar.ohlc.c,
                                    trigger_hint=args.trigger_hint)
        except Exception as e:
            skips[f"error:{type(e).__name__}"] += 1
            continue
        if plan.verdict != "arm":
            skips[plan.skip_reason or "skip"] += 1
            continue
        armed += 1
        future = series[i + 1: i + 1 + args.lookahead]
        outcome, pnl = _label(plan, future, ref_price=decision_bar.ohlc.c)
        s = stats[plan.trigger_kind]
        s["arm"] += 1
        s[outcome] += 1
        s["pnl"] += pnl

    # restore full series
    st._bars[key] = series

    print(f"\n=== grid_levels_sim {args.symbol} {args.tf} "
          f"(decisions {start}->{stop}, stride {args.stride}, lookahead {args.lookahead}) ===")
    print(f"armed: {armed}   skipped: {sum(skips.values())}")
    print("\nskip reasons:")
    for r, c in sorted(skips.items(), key=lambda kv: -kv[1]):
        print(f"  {c:6d}  {r}")
    print("\nedge per trigger kind (full straddle P&L: win=TP net+, loss=TP net- / full-hedge):")
    print(f"  {'kind':12s} {'arm':>6s} {'win':>6s} {'loss':>6s} {'open':>6s} "
          f"{'win%':>7s} {'totPnL':>11s} {'avg/arm':>10s}")
    tot = {"arm": 0, "win": 0, "loss": 0, "open": 0, "pnl": 0.0}
    for kind, s in sorted(stats.items()):
        wr = 100.0 * s["win"] / s["arm"] if s["arm"] else 0.0
        avg = s["pnl"] / s["arm"] if s["arm"] else 0.0
        print(f"  {kind:12s} {int(s['arm']):6d} {int(s['win']):6d} {int(s['loss']):6d} "
              f"{int(s['open']):6d} {wr:6.1f}% {s['pnl']:11.2f} {avg:10.4f}")
        for k in tot:
            tot[k] += s[k]
    wr = 100.0 * tot["win"] / tot["arm"] if tot["arm"] else 0.0
    avg = tot["pnl"] / tot["arm"] if tot["arm"] else 0.0
    print(f"  {'TOTAL':12s} {int(tot['arm']):6d} {int(tot['win']):6d} {int(tot['loss']):6d} "
          f"{int(tot['open']):6d} {wr:6.1f}% {tot['pnl']:11.2f} {avg:10.4f}")
    print("\nNOTE: pnl is price-points×lot (relative). Positive avg/arm = the placement")
    print("has a real neutral-straddle edge before costs; negative = it bleeds in chop.")


if __name__ == "__main__":
    main()
