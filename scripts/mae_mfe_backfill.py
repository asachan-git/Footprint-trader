#!/usr/bin/env python3
"""Tier 0.2 — MAE/MFE backfill on closed positions.

For each closed cycle:
  - Find position open: entry price, side, ts
  - Find cycle close: ts, realized_pnl (R)
  - Walk 1m footprint bars between open_ts and close_ts
  - Compute MFE_R (max favorable excursion in R units)
           MAE_R (max adverse excursion in R units)
  - Compare to realized R

Risk denominator = entry × disaster_floor_pct (stable constant per symbol).
  BTC: 3% of entry.  XAU: 1.5% of entry.
Do NOT use placed SL — it trails during the cycle, making R units shift mid-trade.
Disaster floor is the fixed reference frame that doesn't change after entry.

Answers:
  - If avg MFE >> realized → TP too tight (exits problem)
  - If avg MFE ≈ realized → direction problem (got out near best price)
  - If avg MFE tiny (< 0.2R) → entry filter problem (noise bars, no edge)
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSITIONS = ROOT / "data" / "positions.jsonl"
CYCLES = ROOT / "data" / "cycles.jsonl"
FP_DIR = ROOT / "data" / "footprint"

# Disaster floor per symbol — same constants as grid_placer.py + cycle_manager.py
FLOOR_PCT: dict[str, float] = {
    "BTCUSDT": 0.03,
    "XAUTUSDT": 0.015,
}
DEFAULT_FLOOR_PCT = 0.02


def disaster_risk(symbol: str, entry: float) -> float:
    pct = FLOOR_PCT.get(symbol, DEFAULT_FLOOR_PCT)
    return entry * pct


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.open() if l.strip()] if p.exists() else []


def load_bars_1m(symbol: str) -> list[dict]:
    p = FP_DIR / f"{symbol}_1m.jsonl"
    bars = load_jsonl(p)
    # ensure sorted by close_ts
    bars.sort(key=lambda b: b["close_ts"])
    return bars


def main() -> None:
    positions = load_jsonl(POSITIONS)
    cycles = load_jsonl(CYCLES)

    # cycle_id → (open_ev, close_ev)
    cycle_open: dict[str, dict] = {}
    cycle_close: dict[str, dict] = {}
    for ev in cycles:
        cid = ev.get("cycle_id")
        if ev["type"] == "open":
            cycle_open[cid] = ev
        elif ev["type"] == "close":
            cycle_close[cid] = ev

    # position_id → open event
    pos_open: dict[str, dict] = {}
    for ev in positions:
        if ev.get("type") == "open":
            pos_open[ev["position_id"]] = ev

    # cache bars by symbol
    bars_cache: dict[str, list[dict]] = {}

    rows = []
    for cid, co in cycle_open.items():
        cc = cycle_close.get(cid)
        if not cc:
            continue
        pid = co.get("position_id")
        po = pos_open.get(pid) if pid else None
        if not po:
            continue
        if cc.get("realized_pnl") is None:
            continue

        symbol = po["symbol"]
        side = po["side"]  # long | short
        entry = po["entry"]
        open_ts = co["ts"]
        close_ts = cc["ts"]
        realized_r = cc["realized_pnl"]

        # Stable risk denominator: disaster floor (constant for life of trade).
        # Placed SL trails after BE / +2R, so |entry - SL| collapses toward 0
        # making R units blow up. Disaster floor stays fixed.
        risk = disaster_risk(symbol, entry)
        if risk <= 0:
            continue

        # load bars once per symbol
        if symbol not in bars_cache:
            bars_cache[symbol] = load_bars_1m(symbol)
        bars = bars_cache[symbol]

        # walk bars where close_ts in [open_ts, close_ts]
        # 1m bar close_ts = epoch of bar close. Want bars during the cycle.
        in_window = [b for b in bars if open_ts <= b["close_ts"] <= close_ts + 60]
        if not in_window:
            continue

        highs = [b["ohlc"]["h"] for b in in_window]
        lows = [b["ohlc"]["l"] for b in in_window]
        max_h = max(highs)
        min_l = min(lows)

        if side == "long":
            mfe = (max_h - entry) / risk
            mae = (min_l - entry) / risk  # negative for adverse
        else:
            mfe = (entry - min_l) / risk
            mae = (entry - max_h) / risk

        rows.append({
            "cycle_id": cid,
            "symbol": symbol,
            "side": side,
            "realized_r": realized_r,
            "mfe_r": mfe,
            "mae_r": mae,
            "n_bars": len(in_window),
        })

    if not rows:
        print("No rows generated. Check time alignment between positions/cycles/footprint files.")
        return

    print("=" * 70)
    print("MAE / MFE BACKFILL")
    print("=" * 70)
    print(f"Closed cycles analyzed: {len(rows)}")
    print()

    # NOTE: realized_r from cycles.jsonl had sentinel -1.0 for sl_hit (ingest.py bug,
    # fixed in separate commit). Until new data accumulates, treat realized_r as
    # POISONED for losses. MFE/MAE come from price-walk — always trustworthy.
    sentinel_losses = [r for r in rows if r["realized_r"] == -1.0]
    clean_wins = [r for r in rows if r["realized_r"] > 0]
    clean_losses = [r for r in rows if r["realized_r"] < -1.0]  # real math losses if any
    n_sentinel = len(sentinel_losses)

    mfes = [r["mfe_r"] for r in rows]
    maes = [r["mae_r"] for r in rows]

    print("Aggregate MFE/MAE (R units, disaster-floor denominator):")
    print(f"  MFE  mean: {statistics.mean(mfes):+.3f}  median: {statistics.median(mfes):+.3f}  max: {max(mfes):+.3f}  p90: {sorted(mfes)[int(len(mfes)*0.9)]:+.3f}")
    print(f"  MAE  mean: {statistics.mean(maes):+.3f}  median: {statistics.median(maes):+.3f}  min: {min(maes):+.3f}  p10: {sorted(maes)[int(len(maes)*0.1)]:+.3f}")
    print()
    print(f"  Sentinel losses (realized_r == -1.0, unreliable): {n_sentinel}/{len(rows)}")
    print(f"  Clean wins (reliable):  {len(clean_wins)}")
    print(f"  Clean losses (real R):  {len(clean_losses)}")
    print()

    # MFE distribution — tells entry quality independent of exits
    mfe_buckets = {"<0.05R": 0, "0.05-0.15R": 0, "0.15-0.30R": 0, ">0.30R": 0}
    for m in mfes:
        if m < 0.05:
            mfe_buckets["<0.05R"] += 1
        elif m < 0.15:
            mfe_buckets["0.05-0.15R"] += 1
        elif m < 0.30:
            mfe_buckets["0.15-0.30R"] += 1
        else:
            mfe_buckets[">0.30R"] += 1
    total = len(mfes)
    print("MFE distribution (entry quality — how much in-the-money trades went):")
    for k, n in mfe_buckets.items():
        bar = "█" * int(n / total * 40)
        print(f"  {k:12s}: {n:3d} ({n/total*100:4.0f}%)  {bar}")
    print()

    noise_pct = (mfe_buckets["<0.05R"] + mfe_buckets["0.05-0.15R"]) / total * 100
    print(f"  {noise_pct:.0f}% of cycles peak at < 0.15R  → {'ENTRY FILTER PROBLEM' if noise_pct > 50 else 'acceptable'}")
    print()

    # Wins (reliable)
    if clean_wins:
        win_mfe = statistics.mean(r["mfe_r"] for r in clean_wins)
        win_real = statistics.mean(r["realized_r"] for r in clean_wins)
        capture = win_real / win_mfe * 100 if win_mfe > 0 else 0
        print(f"WINS ({len(clean_wins)} clean — realized R trustworthy):")
        print(f"  avg MFE       : +{win_mfe:.3f}R")
        print(f"  avg realized  : +{win_real:.3f}R")
        print(f"  capture ratio : {capture:.0f}%  {'(TP too tight)' if capture < 60 else '(ok)' if capture < 90 else '(overfit — trailing too far?)'}")
        print()

    # Sentinel losses — MFE is trustworthy, R is not
    if sentinel_losses:
        sl_mfe = statistics.mean(r["mfe_r"] for r in sentinel_losses)
        sl_mae = statistics.mean(r["mae_r"] for r in sentinel_losses)
        n_had_chance = sum(1 for r in sentinel_losses if r["mfe_r"] >= 0.20)
        print(f"SENTINEL LOSSES ({n_sentinel} — realized R = -1.0 sentinel, unreliable):")
        print(f"  avg MFE before loss  : +{sl_mfe:.3f}R  (how far favorable before stopping)")
        print(f"  avg MAE at loss      : {sl_mae:.3f}R   (how deep adverse)")
        print(f"  had MFE ≥ 0.20R first: {n_had_chance}/{n_sentinel} ({n_had_chance/n_sentinel*100:.0f}%)  — were saveable with BE trail")
        print()

    # Diagnostic verdict — based only on trustworthy numbers
    print("=" * 70)
    print("DIAGNOSTIC (based on price-walk MFE/MAE — not sentinel R)")
    print("=" * 70)
    avg_mfe = statistics.mean(mfes)
    if avg_mfe < 0.10:
        print(f"ENTRY FILTER PROBLEM — avg MFE {avg_mfe:.3f}R across all cycles.")
        print("  Price barely moves off entry. Firing on noise bars.")
        print("  Primary fix: Tier 1.6 — cost-aware pre-filter. Gate entries on")
        print("  stacked imbalance OR sweep + VA touch + delta > threshold.")
        print("  Expected: drop ~70% of current trades, lift avg MFE to 0.25R+.")
    elif avg_mfe < 0.20:
        print(f"MIXED — avg MFE {avg_mfe:.3f}R. Some edge, weak signal quality.")
        print("  Both entry filter (Tier 1.6) and direction features (Tier 2B) relevant.")
    else:
        print(f"ENTRY OK — avg MFE {avg_mfe:.3f}R. Price moving meaningfully off entry.")
        if clean_wins:
            capture = statistics.mean(r["realized_r"] for r in clean_wins) / statistics.mean(r["mfe_r"] for r in clean_wins) * 100
            if capture < 60:
                print("  TP PROBLEM (Tier 2A): wins not capturing available R.")
            else:
                print("  DIRECTION PROBLEM (Tier 2B): price moves but wrong direction too often.")

    # Per-symbol
    print()
    print("Per symbol:")
    per_sym: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        per_sym[r["symbol"]].append(r)
    for sym, rs in sorted(per_sym.items()):
        avg_mfe_s = statistics.mean(r["mfe_r"] for r in rs)
        avg_mae_s = statistics.mean(r["mae_r"] for r in rs)
        p90_mfe = sorted(r["mfe_r"] for r in rs)[int(len(rs) * 0.9)]
        noise = sum(1 for r in rs if r["mfe_r"] < 0.10) / len(rs) * 100
        print(f"  {sym}: n={len(rs):3d}  avgMFE={avg_mfe_s:+.3f}R  p90MFE={p90_mfe:+.3f}R  avgMAE={avg_mae_s:+.3f}R  noise<0.10R={noise:.0f}%")


if __name__ == "__main__":
    main()
