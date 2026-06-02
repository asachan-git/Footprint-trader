#!/usr/bin/env python3
"""Synthetic backtest for M2 (rules engine) signals.

For each dry-run M2 signal in mode_compare.jsonl:
  1. Find the 15m bar at signal time → anchor = bar close
  2. Compute ATR(14) from prior 15m bars
  3. Call plan_grid() to get leg1 price, SL, TP  (same logic live would use)
  4. Walk 1m bars from signal bar forward → find first SL or TP touch
  5. Compute realized_r using disaster-floor denominator

Outputs: full P&L breakdown matching bar_verify_outcomes.py format,
split by symbol, bias_strength, score bucket, vote composition.
"""
from __future__ import annotations

import json
import math
import sys
import statistics
from collections import defaultdict
from pathlib import Path

# Add project root to path so imports work
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MC_FILE   = ROOT / "data" / "mode_compare.jsonl"
FP_DIR    = ROOT / "data" / "footprint"

FLOOR_PCT = {"BTCUSDT": 0.03, "XAUTUSDT": 0.015}
BASE_LOT  = {"BTCUSDT": 0.01, "XAUTUSDT": 0.01}


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.open() if l.strip()]


def atr(bars: list[dict], period: int = 14) -> float:
    """True range ATR over last `period` bars."""
    if len(bars) < 2:
        return max(bars[0]["ohlc"]["h"] - bars[0]["ohlc"]["l"], 1.0) if bars else 1.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["ohlc"]["h"], bars[i]["ohlc"]["l"], bars[i-1]["ohlc"]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 1.0


def walk_1m(bars_1m: list[dict], signal_ts: int, sl: float, tp: float,
            side: str) -> dict:
    """Walk 1m bars from signal_ts. Return hit, exit_price, exit_ts."""
    window = [b for b in bars_1m if b["close_ts"] >= signal_ts]
    for bar in window:
        h, l = bar["ohlc"]["h"], bar["ohlc"]["l"]
        if side == "long":
            if l <= sl:
                return {"hit": "sl", "exit_price": sl, "exit_ts": bar["close_ts"]}
            if h >= tp:
                return {"hit": "tp", "exit_price": tp, "exit_ts": bar["close_ts"]}
        else:
            if h >= sl:
                return {"hit": "sl", "exit_price": sl, "exit_ts": bar["close_ts"]}
            if l <= tp:
                return {"hit": "tp", "exit_price": tp, "exit_ts": bar["close_ts"]}
    last = window[-1] if window else None
    return {"hit": "open", "exit_price": last["ohlc"]["c"] if last else sl,
            "exit_ts": last["close_ts"] if last else signal_ts}


def simple_grid(anchor: float, side: str, atr15: float, bias_strength: int,
                symbol: str) -> tuple[float, float, float]:
    """Simplified leg1/SL/TP without full zone_collector (offline-safe).

    Mirrors grid_placer defaults:
      leg1 = anchor  (immediate fill at bar close)
      SL   = anchor ± 5×ATR (safety_sl_atr_mult=5)
      TP   = anchor ± 2×ATR (conservative; real placer uses VP zones)
    """
    sl_dist = atr15 * 5.0
    tp_dist = atr15 * 2.0
    if side == "long":
        sl = anchor - sl_dist
        tp = anchor + tp_dist
    else:
        sl = anchor + sl_dist
        tp = anchor - tp_dist
    return anchor, sl, tp


def try_full_grid(signal: dict, bar: dict, bars_15m: list[dict],
                  settings_stub: dict) -> tuple[float, float, float] | None:
    """Try calling actual plan_grid(). Falls back to simple_grid on any error."""
    try:
        from execution.grid_placer import plan_grid
        from pipeline.features.atr import atr as _atr_fn
        from pipeline.types import Bar, OHLC

        sym    = signal["symbol"]
        side   = signal["side"]
        bs     = int(signal.get("bias_strength", 3))
        anchor = float(bar["ohlc"]["c"])
        atr15  = atr(bars_15m[-20:], 14)
        broker_sym = sym  # paper: same

        plan = plan_grid(
            symbol=sym,
            broker_symbol=broker_sym,
            direction=side,
            anchor=anchor,
            bias_strength=bs,
            atr_15m=atr15,
            base_lot=BASE_LOT.get(sym, 0.01),
            htf_bars=[],
        )
        leg1_price = plan.legs[0].price
        sl         = plan.safety_sl if plan.safety_sl is not None else (anchor - atr15 * 5 if side == "long" else anchor + atr15 * 5)
        tp         = plan.take_profit
        return leg1_price, sl, tp
    except Exception:
        return None


def trend_regime(bars15: list[dict], atr15: float, k_atr: float = 2.0) -> str:
    """Lightweight regime at a signal bar: 20-bar close change normalized by ATR.
    ≥ +k_atr → trend_up, ≤ −k_atr → trend_down, else range. Used to test whether
    the short/long edge survives WITHIN a trend direction or is just the window's
    net drift (regime-confounding)."""
    if len(bars15) < 21 or atr15 <= 0:
        return "range"
    ret = bars15[-1]["ohlc"]["c"] - bars15[-21]["ohlc"]["c"]
    n = ret / atr15
    if n >= k_atr:
        return "trend_up"
    if n <= -k_atr:
        return "trend_down"
    return "range"


def main() -> None:
    signals = load_jsonl(MC_FILE)
    fired   = [s for s in signals if s.get("side") not in ("flat", None)
               and s.get("dry_run") and s.get("bar_id")]

    # Load bars
    bars_15m: dict[str, list[dict]] = {}
    bars_1m:  dict[str, list[dict]] = {}
    for sym in ["BTCUSDT", "XAUTUSDT"]:
        b15 = load_jsonl(FP_DIR / f"{sym}_15m.jsonl")
        b1  = load_jsonl(FP_DIR / f"{sym}_1m.jsonl")
        b15.sort(key=lambda b: b["close_ts"])
        b1.sort(key=lambda b: b["close_ts"])
        bars_15m[sym] = b15
        bars_1m[sym]  = b1

    # Index 15m bars by close_ts for fast lookup
    idx_15m: dict[str, dict[int, dict]] = {
        sym: {b["close_ts"]: b for b in bars_15m[sym]}
        for sym in bars_15m
    }

    rows = []
    skipped = 0

    for sig in fired:
        sym  = sig["symbol"]
        side = sig["side"]
        ts   = int(sig["ts"])
        bs   = int(sig.get("bias_strength", 3))

        # Find the bar this signal was computed on
        # bar_id = "SYMBOL|15m|close_ts" — parse close_ts
        bar_id_ts = int(sig["bar_id"].split("|")[2])
        bar = idx_15m.get(sym, {}).get(bar_id_ts)
        if bar is None:
            skipped += 1
            continue

        # Bars up to and including this bar for ATR
        prior_15m = [b for b in bars_15m[sym] if b["close_ts"] <= bar_id_ts]
        atr15 = atr(prior_15m[-20:], 14) if len(prior_15m) >= 2 else 1.0

        # Try full grid_placer first; fall back to simple
        grid_result = try_full_grid(sig, bar, prior_15m, {})
        if grid_result is not None:
            entry, sl, tp = grid_result
            grid_method = "full"
        else:
            entry, sl, tp = simple_grid(float(bar["ohlc"]["c"]), side, atr15, bs, sym)
            grid_method = "simple"

        # Validate SL/TP on profit side
        if side == "long" and (tp <= entry or sl >= entry):
            skipped += 1
            continue
        if side == "short" and (tp >= entry or sl <= entry):
            skipped += 1
            continue

        risk = entry * FLOOR_PCT.get(sym, 0.02)
        if risk <= 0:
            skipped += 1
            continue

        # Walk 1m bars from signal bar
        result = walk_1m(bars_1m[sym], bar_id_ts, sl, tp, side)
        hit        = result["hit"]
        exit_price = result["exit_price"]

        if side == "long":
            realized_r = (exit_price - entry) / risk
        else:
            realized_r = (entry - exit_price) / risk

        # Vote composition
        votes      = sig.get("votes") or []
        vote_keys  = [v.get("m", "?") for v in votes]
        score      = float(sig.get("score", 0))

        rows.append({
            "sym":        sym,
            "side":       side,
            "ts":         ts,
            "bs":         bs,
            "score":      score,
            "entry":      entry,
            "sl":         sl,
            "tp":         tp,
            "hit":        hit,
            "exit_price": exit_price,
            "realized_r": realized_r,
            "atr15":      atr15,
            "vote_keys":  vote_keys,
            "grid_method": grid_method,
            "regime":     trend_regime(prior_15m, atr15),
        })

    # ── Report ───────────────────────────────────────────────────────────────
    print("=" * 70)
    print("M2 RULES ENGINE — SYNTHETIC BACKTEST")
    print("=" * 70)
    print(f"Signals processed : {len(fired)}")
    print(f"Skipped (no bar)  : {skipped}")
    print(f"Backtested        : {len(rows)}")
    gm = defaultdict(int)
    for r in rows: gm[r["grid_method"]] += 1
    print(f"Grid method       : full={gm['full']}  simple={gm['simple']}")
    print()

    if not rows:
        print("No rows — check bar file coverage.")
        return

    tp_rows = [r for r in rows if r["hit"] == "tp"]
    sl_rows = [r for r in rows if r["hit"] == "sl"]
    op_rows = [r for r in rows if r["hit"] == "open"]
    all_r   = [r["realized_r"] for r in rows]
    wins    = sum(1 for r in all_r if r > 0)

    print(f"TP hits   : {len(tp_rows)} ({len(tp_rows)/len(rows)*100:.0f}%)")
    print(f"SL hits   : {len(sl_rows)} ({len(sl_rows)/len(rows)*100:.0f}%)")
    print(f"No hit    : {len(op_rows)}")
    print()
    print(f"Win Rate  : {wins}/{len(rows)} = {wins/len(rows)*100:.0f}%")
    print(f"Total R   : {sum(all_r):+.3f}")
    print(f"Avg R     : {statistics.mean(all_r):+.4f}")
    print(f"Median R  : {statistics.median(all_r):+.4f}")
    if len(all_r) > 1:
        print(f"StdDev R  : {statistics.stdev(all_r):.4f}")
    print()

    # Per symbol
    print("Per symbol:")
    for sym in ["BTCUSDT", "XAUTUSDT"]:
        rs = [r for r in rows if r["sym"] == sym]
        if not rs: continue
        wr = sum(1 for r in rs if r["realized_r"] > 0) / len(rs)
        sr = sum(r["realized_r"] for r in rs)
        print(f"  {sym}: n={len(rs):3d}  WR={wr*100:.0f}%  totalR={sr:+.3f}  avgR={sr/len(rs):+.4f}")
    print()

    # Per bias_strength
    print("Per bias_strength (1=weak signal, 5=strong):")
    for bs in range(1, 6):
        rs = [r for r in rows if r["bs"] == bs]
        if not rs: continue
        wr = sum(1 for r in rs if r["realized_r"] > 0) / len(rs)
        sr = sum(r["realized_r"] for r in rs)
        print(f"  bs={bs}: n={len(rs):3d}  WR={wr*100:.0f}%  totalR={sr:+.3f}  avgR={sr/len(rs):+.4f}")
    print()

    # Score buckets
    print("Per score bucket:")
    def score_bucket(s):
        if s <= -0.5: return "strong_short (<=-0.5)"
        if s < 0:     return "weak_short  (-0.5..0)"
        if s == 0:    return "neutral     (0)"
        if s <= 0.5:  return "weak_long   (0..0.5)"
        return              "strong_long (>0.5)"
    sbuckets = defaultdict(list)
    for r in rows:
        sbuckets[score_bucket(r["score"])].append(r["realized_r"])
    for k in ["strong_short (<=-0.5)", "weak_short  (-0.5..0)",
              "weak_long   (0..0.5)", "strong_long (>0.5)"]:
        rs = sbuckets[k]
        if not rs: continue
        wr = sum(1 for r in rs if r > 0) / len(rs)
        print(f"  {k}: n={len(rs):3d}  WR={wr*100:.0f}%  totalR={sum(rs):+.3f}  avgR={sum(rs)/len(rs):+.4f}")
    print()

    # ── Regime × side — does the directional edge survive within a regime, or
    # is it just the window's net drift? (the key confound check) ──────────────
    print("Regime × side (does short/long edge survive within a trend?):")
    print(f"  {'regime':12s} {'side':6s} {'n':>4s} {'WR':>5s} {'avgR':>8s}")
    reg_side = defaultdict(list)
    for r in rows:
        reg_side[(r["regime"], r["side"])].append(r["realized_r"])
    for reg in ("trend_up", "range", "trend_down"):
        for sd in ("long", "short"):
            rs = reg_side.get((reg, sd), [])
            if not rs:
                continue
            wr = sum(1 for x in rs if x > 0) / len(rs)
            print(f"  {reg:12s} {sd:6s} {len(rs):>4d} {wr*100:>4.0f}% {sum(rs)/len(rs):>+8.4f}")
    # regime mix (is the sample dominated by one regime?)
    reg_mix = defaultdict(int)
    for r in rows:
        reg_mix[r["regime"]] += 1
    print("  regime mix:", {k: reg_mix[k] for k in ("trend_up", "range", "trend_down")})
    print()

    # ── Vote × side avgR — is the long-side weakness a specific broken vote? ──
    print("Vote × side (avgR of signals where the vote is present):")
    print(f"  {'module':20s} {'long n/avgR':>16s}   {'short n/avgR':>16s}")
    vs = defaultdict(lambda: {"long": [], "short": []})
    for r in rows:
        for v in r["vote_keys"]:
            vs[v][r["side"]].append(r["realized_r"])
    for v in sorted(vs, key=lambda k: -(len(vs[k]['long']) + len(vs[k]['short']))):
        lo, sh = vs[v]["long"], vs[v]["short"]
        lo_s = f"{len(lo):>3d}/{sum(lo)/len(lo):+.3f}" if lo else "   —"
        sh_s = f"{len(sh):>3d}/{sum(sh)/len(sh):+.3f}" if sh else "   —"
        print(f"  {v:20s} {lo_s:>16s}   {sh_s:>16s}")
    print()

    # Vote composition — which votes appear most in winning vs losing signals
    print("Vote composition (top modules by frequency):")
    vote_freq: dict[str, dict[str, int]] = defaultdict(lambda: {"win": 0, "loss": 0})
    for r in rows:
        outcome = "win" if r["realized_r"] > 0 else "loss"
        for v in r["vote_keys"]:
            vote_freq[v][outcome] += 1
    sorted_votes = sorted(vote_freq.items(), key=lambda x: -(x[1]["win"] + x[1]["loss"]))
    for v, counts in sorted_votes[:10]:
        total = counts["win"] + counts["loss"]
        wr = counts["win"] / total if total else 0
        print(f"  {v:20s}: n={total:3d}  WR={wr*100:.0f}%  (win={counts['win']} loss={counts['loss']})")
    print()

    # R distribution
    print("R distribution:")
    buckets = defaultdict(int)
    for r in all_r:
        if r < -0.5:   k = "< -0.5R"
        elif r < -0.1: k = "-0.5 to -0.1R"
        elif r < 0:    k = "-0.1 to 0R"
        elif r < 0.05: k = "0 to 0.05R"
        elif r < 0.15: k = "0.05 to 0.15R"
        elif r < 0.5:  k = "0.15 to 0.5R"
        else:          k = "> 0.5R"
        buckets[k] += 1
    n = len(all_r)
    for k in ["< -0.5R", "-0.5 to -0.1R", "-0.1 to 0R", "0 to 0.05R",
              "0.05 to 0.15R", "0.15 to 0.5R", "> 0.5R"]:
        v = buckets[k]
        bar_str = "█" * int(v / n * 40)
        print(f"  {k:18s}: {v:3d} ({v/n*100:4.0f}%)  {bar_str}")
    print()

    # M1 vs M2 comparison (M1 benchmark from bar_verify results)
    print("=" * 70)
    print("M2 vs M1 COMPARISON (M1 from bar-verified actuals)")
    print("=" * 70)
    print(f"{'Metric':<22} {'M1 actual':>12} {'M2 synthetic':>12}")
    print("-" * 48)
    print(f"{'n trades':<22} {'109':>12} {len(rows):>12}")
    print(f"{'Win Rate':<22} {'93%':>12} {wins/len(rows)*100:>11.0f}%")
    print(f"{'Total R':<22} {'+5.452':>12} {sum(all_r):>+12.3f}")
    print(f"{'Avg R/trade':<22} {'+0.0500':>12} {statistics.mean(all_r):>+12.4f}")
    print(f"{'Median R':<22} {'+0.0519':>12} {statistics.median(all_r):>+12.4f}")


if __name__ == "__main__":
    main()
