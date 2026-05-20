"""VP Verification Script — shows exactly how VP is calculated for a symbol/date.

Shows: bars included, price-volume ladder, POC math, value area walkthrough.

Usage:
  python3 scripts/verify_vp.py --symbol BTCUSDT --date 2026-05-19
  python3 scripts/verify_vp.py --symbol XAUTUSDT             # today
  python3 scripts/verify_vp.py --symbol BTCUSDT --period weekly
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.state_store import store
from pipeline.features.vp_cache import _day_bounds, _week_bounds, _compute_period_vp, _utc_session_date
from pipeline.features.volume_profile import (
    _build_price_map, _build_bins, _classify_shape, _value_area,
    _find_hvn_zones, _find_lvn_zones, NUM_BINS, VALUE_AREA_PCT,
)


SESSION_ANCHOR = {
    "BTCUSDT": 0,
    "XAUTUSDT": 0,   # Bybit 24/7, 00:00 UTC daily roll (5:30 AM IST)
}

IST_OFFSET = 5 * 3600 + 30 * 60


IST_TZ = timezone(timedelta(hours=5, minutes=30))


def ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST_TZ).strftime("%Y-%m-%d %H:%M IST")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--period", default="daily", choices=["daily", "weekly"])
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--top-n", type=int, default=30, help="Show top N price levels by volume")
    args = ap.parse_args()

    sess_start = SESSION_ANCHOR.get(args.symbol, 0)
    s = store()
    all_bars = s.recent(args.symbol, args.tf, 100_000)

    if not all_bars:
        print(f"No bars for {args.symbol} {args.tf} in state_store.")
        return

    # Determine period bounds
    if args.date is None:
        from datetime import date
        args.date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if args.period == "daily":
        start_ts, end_ts = _day_bounds(args.date, sess_start)
        period_label = f"Daily {args.date}"
    else:
        year, week = datetime.strptime(args.date, "%Y-%m-%d").isocalendar()[:2]
        week_key = f"{year}-W{week:02d}"
        start_ts, end_ts = _week_bounds(week_key, sess_start)
        period_label = f"Weekly {week_key}"

    now_ts = int(__import__("time").time())
    effective_end = min(end_ts, now_ts)

    period_bars = [b for b in all_bars if start_ts <= b.close_ts < effective_end]

    print(f"\n{'='*60}")
    print(f"  VP VERIFICATION: {args.symbol} — {period_label}")
    print(f"{'='*60}")
    actual_utc_hour = datetime.fromtimestamp(start_ts, tz=timezone.utc).hour
    if isinstance(sess_start, dict):
        anchor_label = f"{sess_start['hour']:02d}:00 {sess_start['tz']}"
    else:
        anchor_label = f"{actual_utc_hour:02d}:00 UTC"
    print(f"  Session anchor: {anchor_label}  (resolved to {actual_utc_hour:02d}:00 UTC for this date)")
    print(f"  Period start: {ist(start_ts)}  (unix: {start_ts})")
    print(f"  Period end:   {ist(effective_end)}  (unix: {effective_end})")
    print(f"  Bars included: {len(period_bars)} x {args.tf}")

    if not period_bars:
        print("\n  ⚠ No bars found for this period.")
        print(f"  Available range: {ist(all_bars[0].close_ts)} → {ist(all_bars[-1].close_ts)}")
        return

    # Show bar time range
    print(f"  Bar range: {ist(period_bars[0].close_ts)} → {ist(period_bars[-1].close_ts)}")

    # Sparse ladder map (for the per-tick chart only)
    vol_map = _build_price_map(period_bars)

    # Tick-aligned bin size from volume_profile resolver (settings.yaml → defaults)
    from pipeline.features.volume_profile import _resolve_bin_size
    bin_size_cfg = _resolve_bin_size(args.symbol)

    built = _build_bins(period_bars, bin_size=bin_size_cfg)
    if built is None:
        print("\n  ⚠ Cannot compute VP — no valid bars")
        return
    bins, _bin_centers, bin_size, p_min, p_max = built
    n_bins = len(bins)
    total_vol = float(bins.sum())

    print(f"\n  Total volume: {total_vol:.2f}")
    mode_label = "tick-aligned" if bin_size_cfg else "legacy range/68"
    print(f"  Bins: {n_bins} × {bin_size:.3f}pt  (range {p_max - p_min:.1f}pt, {mode_label})")
    print(f"  Distinct ladder prices: {len(vol_map)}")

    # POC + value area
    max_v = float(bins.max())
    tied = [i for i in range(n_bins) if bins[i] == max_v]
    poc_idx = tied[len(tied) // 2]
    lo_idx, hi_idx = _value_area(bins, poc_idx, VALUE_AREA_PCT)
    if bin_size_cfg:
        poc = round(p_min + poc_idx * bin_size, 4)
    else:
        poc = round(p_min + (poc_idx + 0.5) * bin_size, 2)
    val = round(p_min + lo_idx * bin_size, 4)
    vah = round(p_min + (hi_idx + 1) * bin_size, 4)
    shape = _classify_shape(bins, poc_idx, lo_idx, hi_idx, n_bins)
    hvn = _find_hvn_zones(bins, bin_size, p_min)
    lvn = _find_lvn_zones(bins, bin_size, p_min)

    print(f"\n{'─'*60}")
    print(f"  POC:   {poc:.2f}  (price with highest volume)")
    print(f"  VAH:   {vah:.2f}  (70% value area high)")
    print(f"  VAL:   {val:.2f}  (70% value area low)")
    print(f"  Shape: {shape}")
    hvn_str = [f"{z['low']:.1f}-{z['high']:.1f}" for z in hvn[:5]]
    lvn_str = [f"{z['low']:.1f}-{z['high']:.1f}" for z in lvn[:5]]
    print(f"  HVN zones: {hvn_str}")
    print(f"  LVN zones: {lvn_str}")

    # Current price position
    latest_close = period_bars[-1].ohlc.c
    if latest_close > vah:
        position = "ABOVE_VAH (breakout / overextension)"
    elif latest_close < val:
        position = "BELOW_VAL (breakdown)"
    elif abs(latest_close - poc) <= (vah - val) * 0.05:
        position = "AT_POC (fair value)"
    elif latest_close > poc:
        position = "IN_VA_ABOVE_POC (bullish inside VA)"
    else:
        position = "IN_VA_BELOW_POC (bearish inside VA)"
    print(f"  Current price: {latest_close:.2f} → {position}")

    # Top N price levels by volume — ASCII chart
    print(f"\n{'─'*60}")
    print(f"  Price-Volume Ladder (top {args.top_n} levels):")
    print(f"  {'Price':>10}  {'Volume':>10}  {'% of total':>10}  Chart")
    print(f"  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*20}")

    sorted_prices = sorted(vol_map.keys(), reverse=True)
    # Include POC, VAH, VAL + top N by volume
    top_by_vol = sorted(vol_map.keys(), key=lambda p: vol_map[p], reverse=True)[:args.top_n]
    show_prices = sorted(set(top_by_vol) | {poc, vah, val}, reverse=True)

    max_v = max(vol_map.values()) if vol_map else 1
    for p in show_prices:
        v = vol_map.get(p, 0)
        bar_len = int(v / max_v * 20)
        bar = "█" * bar_len
        pct = v / total_vol * 100
        tag = ""
        if p == poc: tag = " ◀ POC"
        elif p == vah: tag = " ◀ VAH"
        elif p == val: tag = " ◀ VAL"
        is_hvn = any(z["low"] <= p <= z["high"] for z in hvn)
        is_lvn = any(z["low"] <= p <= z["high"] for z in lvn)
        if is_hvn: tag += " [HVN]"
        if is_lvn: tag += " [LVN]"
        print(f"  {p:>10.2f}  {v:>10.2f}  {pct:>9.1f}%  {bar}{tag}")

    # Value area walkthrough
    print(f"\n{'─'*60}")
    print("  Value Area calculation (walk from POC outward to 70%):")
    prices = sorted(vol_map.keys())
    vols = [vol_map[p] for p in prices]
    poc_idx = prices.index(poc)
    lo = hi = poc_idx
    acc = vols[poc_idx]
    target = total_vol * 0.70
    step = 0
    print(f"  Step 0: start at POC {poc:.2f}, vol={vols[poc_idx]:.2f}, acc={acc:.2f}/{target:.2f} ({acc/total_vol*100:.1f}%)")
    while acc < target and step < 20:
        step += 1
        left = vols[lo - 1] if lo > 0 else -1
        right = vols[hi + 1] if hi < len(prices) - 1 else -1
        if right >= left and hi < len(prices) - 1:
            hi += 1
            acc += vols[hi]
            print(f"  Step {step}: expand UP to {prices[hi]:.2f}, add {vols[hi]:.2f} → acc={acc:.2f} ({acc/total_vol*100:.1f}%)")
        elif lo > 0:
            lo -= 1
            acc += vols[lo]
            print(f"  Step {step}: expand DOWN to {prices[lo]:.2f}, add {vols[lo]:.2f} → acc={acc:.2f} ({acc/total_vol*100:.1f}%)")
        else:
            break
    print(f"  Final VA: {prices[lo]:.2f} – {prices[hi]:.2f} covers {acc/total_vol*100:.1f}% of volume")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
