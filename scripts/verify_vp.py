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
from pipeline.features.volume_profile import _build_price_map, _compute_poc_va, _detect_shape, _hvn_lvn


SESSION_START = {
    "BTCUSDT": 0,    # 00:00 UTC = 5:30 AM IST
    "XAUTUSDT": 22,  # 22:00 UTC = 3:30 AM IST next day
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

    sess_start = SESSION_START.get(args.symbol, 0)
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
    sess_ist_h = (sess_start + 5) % 24
    sess_ist_label = f"{sess_ist_h:02d}:30 IST"
    print(f"  Session start UTC: {sess_start:02d}:00  (= {sess_ist_label})")
    print(f"  Period start: {ist(start_ts)}  (unix: {start_ts})")
    print(f"  Period end:   {ist(effective_end)}  (unix: {effective_end})")
    print(f"  Bars included: {len(period_bars)} x {args.tf}")

    if not period_bars:
        print("\n  ⚠ No bars found for this period.")
        print(f"  Available range: {ist(all_bars[0].close_ts)} → {ist(all_bars[-1].close_ts)}")
        return

    # Show bar time range
    print(f"  Bar range: {ist(period_bars[0].close_ts)} → {ist(period_bars[-1].close_ts)}")

    # Build price-volume map
    vol_map = _build_price_map(period_bars)
    total_vol = sum(vol_map.values())
    avg_vol = total_vol / len(vol_map) if vol_map else 0

    print(f"\n  Total volume: {total_vol:.2f}")
    print(f"  Price levels: {len(vol_map)}")
    print(f"  Avg vol/level: {avg_vol:.2f}")

    # POC + value area
    result = _compute_poc_va(vol_map)
    if not result:
        print("\n  ⚠ Cannot compute POC — empty volume map")
        return
    poc, vah, val = result
    shape = _detect_shape(vol_map, poc)
    hvn, lvn = _hvn_lvn(vol_map)

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
