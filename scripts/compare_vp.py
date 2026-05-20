"""3-way Volume Profile algorithm comparison harness.

Runs three algorithms on the same session bars and prints them side-by-side:

  A — Sparse-ladder (from git commit 6c9bf0c, before the scipy rewrite).
      Aggregates every tick price from bid+ask ladders, picks POC at the
      single price with max volume, walks VA via tick-level pair-test.
  B — Gaussian dense-grid (in-session, never committed).
      Builds a dense regular grid (~150 bins), Gaussian-smooths, finds HVN
      as contiguous runs ≥ 0.65 × POC peak with prominence ≥ 20%, LVN as
      valleys strictly between adjacent HVNs.
  C — Current scipy-based (uniform bar distribution + find_peaks).
      Calls pipeline.features.volume_profile.compute() directly, picking
      up the tick-aligned bin_size from settings.yaml.

Both legacy algorithms (A, B) are vendored locally — they do NOT pollute
the production module. Switching algorithms in production happens by
swapping volume_profile.compute(); this script is purely for inspection.

Usage:
  python3 scripts/compare_vp.py --symbol XAUTUSDT --date 2026-05-19
  python3 scripts/compare_vp.py --symbol BTCUSDT  --date 2026-05-19
  python3 scripts/compare_vp.py --symbol XAUTUSDT --date 2026-05-19 --bin-size 0.5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.state_store import store
from pipeline.features.vp_cache import _day_bounds


SESSION_ANCHOR = {
    "BTCUSDT": 0,
    "XAUTUSDT": 0,   # Bybit 24/7, 00:00 UTC daily roll (5:30 AM IST)
}


# ════════════════════════════════════════════════════════════════════════════
# Method A — Sparse-ladder (git 6c9bf0c)
# ════════════════════════════════════════════════════════════════════════════

def _build_price_map_A(bars) -> dict[float, float]:
    vol_map: dict[float, float] = {}
    for bar in bars:
        for lvl in bar.bid_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
        for lvl in bar.ask_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
    return vol_map


def _poc_va_A(vol_map: dict[float, float], va_pct: float = 0.70):
    if not vol_map:
        return None
    prices = sorted(vol_map.keys())
    total = sum(vol_map.values())
    poc = max(prices, key=lambda p: vol_map[p])
    poc_idx = prices.index(poc)
    target = total * va_pct
    lo = hi = poc_idx
    acc = vol_map[poc]
    while acc < target and (lo > 0 or hi < len(prices) - 1):
        left = vol_map[prices[lo - 1]] if lo > 0 else -1
        right = vol_map[prices[hi + 1]] if hi < len(prices) - 1 else -1
        if right >= left and hi < len(prices) - 1:
            hi += 1
            acc += vol_map[prices[hi]]
        elif lo > 0:
            lo -= 1
            acc += vol_map[prices[lo]]
        else:
            break
    return poc, prices[hi], prices[lo]


def _group_levels_into_zones_A(prices: list[float], price_step: float) -> list[dict]:
    if not prices:
        return []
    prices = sorted(prices)
    zones: list[dict] = []
    zone_start = zone_end = prices[0]
    for p in prices[1:]:
        if p - zone_end <= price_step * 2:
            zone_end = p
        else:
            zones.append({"low": zone_start, "high": zone_end})
            zone_start = zone_end = p
    zones.append({"low": zone_start, "high": zone_end})
    return zones


def _hvn_lvn_A(vol_map: dict[float, float]) -> tuple[list[dict], list[dict]]:
    if not vol_map or len(vol_map) < 2:
        return [], []
    prices = sorted(vol_map.keys())
    price_step = min(prices[i + 1] - prices[i] for i in range(len(prices) - 1)) or 1.0
    avg = sum(vol_map.values()) / len(vol_map)
    hvn_prices = [p for p, v in vol_map.items() if v >= avg * 1.5]
    lvn_prices = [p for p, v in vol_map.items() if 0 < v <= avg * 0.5]
    return (
        _group_levels_into_zones_A(hvn_prices, price_step),
        _group_levels_into_zones_A(lvn_prices, price_step),
    )


def _shape_A(vol_map: dict[float, float], poc: float) -> str:
    if len(vol_map) < 5:
        return "thin"
    prices = sorted(vol_map.keys())
    total = sum(vol_map.values())
    if total == 0 or prices[-1] == prices[0]:
        return "thin"
    centroid = sum(p * v for p, v in vol_map.items()) / total
    rel_pos = (centroid - prices[0]) / (prices[-1] - prices[0])
    if rel_pos > 0.60:
        return "P"
    if rel_pos < 0.40:
        return "b"
    return "D"


def method_A(bars) -> dict:
    vol_map = _build_price_map_A(bars)
    if not vol_map:
        return {"poc": None, "vah": None, "val": None, "shape": "thin", "hvn": [], "lvn": []}
    res = _poc_va_A(vol_map)
    poc, vah, val = res if res else (None, None, None)
    hvn, lvn = _hvn_lvn_A(vol_map)
    shape = _shape_A(vol_map, poc) if poc else "thin"
    return {
        "poc": round(poc, 4) if poc else None,
        "vah": round(vah, 4) if vah else None,
        "val": round(val, 4) if val else None,
        "shape": shape, "hvn": hvn, "lvn": lvn,
        "info": "sparse ladder, 70% VA, single-tick POC",
    }


# ════════════════════════════════════════════════════════════════════════════
# Method B — Gaussian dense-grid (mid-session, never committed)
# ════════════════════════════════════════════════════════════════════════════

def _dense_grid_B(vol_map: dict[float, float], target_bins: int = 150):
    if not vol_map:
        return None
    prices = sorted(vol_map.keys())
    p_min, p_max = prices[0], prices[-1]
    if p_max == p_min:
        return None
    diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    tick = min(d for d in diffs if d > 0) if diffs else 1.0
    span = p_max - p_min
    bw = max(tick, span / target_bins)
    n = max(1, int(round(span / bw)) + 1)
    centers = np.array([p_min + i * bw for i in range(n)])
    vols = np.zeros(n)
    for p, v in vol_map.items():
        idx = max(0, min(n - 1, int(round((p - p_min) / bw))))
        vols[idx] += v
    return centers, vols, bw, p_min, p_max


def _gaussian_smooth_B(vols: np.ndarray, sigma: float) -> np.ndarray:
    if len(vols) == 0 or sigma <= 0:
        return vols.copy()
    radius = max(1, int(math.ceil(3 * sigma)))
    kernel = np.array([math.exp(-(i * i) / (2 * sigma * sigma)) for i in range(-radius, radius + 1)])
    out = np.zeros_like(vols)
    n = len(vols)
    for i in range(n):
        acc = 0.0
        wsum = 0.0
        for k, off in enumerate(range(-radius, radius + 1)):
            j = i + off
            if 0 <= j < n:
                acc += vols[j] * kernel[k]
                wsum += kernel[k]
        out[i] = acc / wsum if wsum > 0 else 0.0
    return out


def _contiguous_above(vols: np.ndarray, thresh: float) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    n = len(vols)
    i = 0
    while i < n:
        if vols[i] >= thresh:
            j = i
            while j < n and vols[j] >= thresh:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def method_B(bars) -> dict:
    vol_map = _build_price_map_A(bars)  # same sparse ladder input
    grid = _dense_grid_B(vol_map)
    if grid is None:
        return {"poc": None, "vah": None, "val": None, "shape": "thin", "hvn": [], "lvn": []}
    centers, vols, bw, p_min, p_max = grid
    n = len(vols)
    sigma = max(1.5, n * 0.015)
    sm = _gaussian_smooth_B(vols, sigma)
    peak_v = float(sm.max())
    if peak_v <= 0:
        return {"poc": None, "vah": None, "val": None, "shape": "thin", "hvn": [], "lvn": []}

    # POC + VA (smoothed pair-test, 70% VA — match original mid-session version)
    poc_idx = int(np.argmax(sm))
    total = float(sm.sum())
    target = total * 0.70
    lo = hi = poc_idx
    acc = float(sm[poc_idx])
    while acc < target and (lo > 0 or hi < n - 1):
        up_can = hi < n - 1
        down_can = lo > 0
        up = (sm[hi + 1] if up_can else 0.0) + (sm[hi + 2] if hi + 2 < n else 0.0)
        dn = (sm[lo - 1] if down_can else 0.0) + (sm[lo - 2] if lo - 2 >= 0 else 0.0)
        if up_can and (not down_can or up >= dn):
            hi += 1
            acc += float(sm[hi])
        elif down_can:
            lo -= 1
            acc += float(sm[lo])
        else:
            break

    half_bw = bw / 2.0
    poc = round(float(centers[poc_idx]), 4)
    vah = round(float(centers[hi]) + half_bw, 4)
    val = round(float(centers[lo]) - half_bw, 4)

    # HVN: contiguous bins ≥ 0.65 × POC peak with prominence ≥ 20%
    runs = _contiguous_above(sm, peak_v * 0.65)
    min_run = max(2, int(round(n * 0.005)))
    hvn_bands: list[dict] = []
    for lo_r, hi_r in runs:
        if (hi_r - lo_r + 1) < min_run and not (lo_r <= poc_idx <= hi_r):
            continue
        peak = float(sm[lo_r:hi_r + 1].max())
        win = max(5, n // 10)
        left_min = float(sm[max(0, lo_r - win):lo_r].min()) if lo_r > 0 else 0.0
        right_min = float(sm[hi_r + 1:min(n, hi_r + 1 + win)].min()) if hi_r < n - 1 else 0.0
        prom = peak - max(left_min, right_min)
        if prom < peak_v * 0.20 and not (lo_r <= poc_idx <= hi_r):
            continue
        hvn_bands.append({
            "lo": lo_r, "hi": hi_r, "peak": peak,
            "low": round(float(centers[lo_r]) - half_bw, 4),
            "high": round(float(centers[hi_r]) + half_bw, 4),
            "total": float(sm[lo_r:hi_r + 1].sum()),
        })
    # Force POC band if missing
    if not any(b["lo"] <= poc_idx <= b["hi"] for b in hvn_bands):
        guard = peak_v * 0.50
        lo_b = hi_b = poc_idx
        while lo_b > 0 and sm[lo_b - 1] >= guard:
            lo_b -= 1
        while hi_b < n - 1 and sm[hi_b + 1] >= guard:
            hi_b += 1
        hvn_bands.append({
            "lo": lo_b, "hi": hi_b, "peak": float(sm[poc_idx]),
            "low": round(float(centers[lo_b]) - half_bw, 4),
            "high": round(float(centers[hi_b]) + half_bw, 4),
            "total": float(sm[lo_b:hi_b + 1].sum()),
        })
    hvn_bands.sort(key=lambda b: b["total"], reverse=True)
    hvn_bands = hvn_bands[:5]
    hvn_bands.sort(key=lambda b: b["lo"])

    # LVN: valleys strictly between adjacent HVN bands
    lvn_zones: list[dict] = []
    for i_h in range(len(hvn_bands) - 1):
        left, right = hvn_bands[i_h], hvn_bands[i_h + 1]
        gap_lo = left["hi"] + 1
        gap_hi = right["lo"] - 1
        if gap_hi < gap_lo:
            continue
        ref = min(left["peak"], right["peak"])
        thresh = ref * 0.40
        valley_lo = valley_hi = None
        i_v = gap_lo
        while i_v <= gap_hi:
            if sm[i_v] <= thresh:
                j = i_v
                while j <= gap_hi and sm[j] <= thresh:
                    j += 1
                if valley_lo is None or (j - i_v) > (valley_hi - valley_lo):
                    valley_lo, valley_hi = i_v, j - 1
                i_v = j
            else:
                i_v += 1
        if valley_lo is not None and valley_hi is not None and (valley_hi - valley_lo + 1) >= 2:
            lvn_zones.append({
                "low": round(float(centers[valley_lo]) - half_bw, 4),
                "high": round(float(centers[valley_hi]) + half_bw, 4),
            })

    return {
        "poc": poc, "vah": vah, "val": val,
        "shape": "double" if len(hvn_bands) >= 2 else "D",
        "hvn": [{"low": b["low"], "high": b["high"]} for b in hvn_bands],
        "lvn": lvn_zones,
        "info": f"dense {n} bins × {bw:.3f}pt, Gaussian σ={sigma:.1f}",
    }


# ════════════════════════════════════════════════════════════════════════════
# Method C — Current scipy uniform-distribution (production)
# ════════════════════════════════════════════════════════════════════════════

def method_C(bars, bin_size: float | None) -> dict:
    from pipeline.features.volume_profile import compute
    vp = compute(bars, "compare", bars[-1].ohlc.c, bin_size=bin_size)
    return {
        "poc": vp.poc, "vah": vp.vah, "val": vp.val,
        "shape": vp.shape,
        "hvn": vp.hvn_zones, "lvn": vp.lvn_zones,
        "info": f"current scipy, bin_size={bin_size or 'legacy/68'}",
    }


# ════════════════════════════════════════════════════════════════════════════
# CLI / output
# ════════════════════════════════════════════════════════════════════════════

def _fmt_zones(zones: list[dict], n: int = 4) -> str:
    if not zones:
        return "—"
    out = []
    for z in zones[:n]:
        out.append(f"{z['low']:g}–{z['high']:g}")
    extra = f" (+{len(zones) - n} more)" if len(zones) > n else ""
    return ", ".join(out) + extra


def _fmt_num(x) -> str:
    if x is None:
        return "—"
    return f"{x:g}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUTUSDT")
    ap.add_argument("--date", required=True, help="IST session date YYYY-MM-DD")
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--bin-size", type=float, default=None,
                    help="Override Method C bin_size; defaults to settings.yaml resolution")
    args = ap.parse_args()

    anchor = SESSION_ANCHOR.get(args.symbol, 0)
    s = store()
    all_bars = s.recent(args.symbol, args.tf, 100_000)
    if not all_bars:
        print(f"No bars in state_store for {args.symbol}")
        return

    start_ts, end_ts = _day_bounds(args.date, anchor)
    bars = [b for b in all_bars if start_ts <= b.close_ts < end_ts]
    if len(bars) < 30:
        print(f"Only {len(bars)} bars for {args.symbol} {args.date} — skipping")
        return

    # Resolve Method C bin_size: CLI override > settings.yaml > defaults
    if args.bin_size is not None:
        c_bin_size: float | None = args.bin_size
    else:
        from pipeline.features.volume_profile import _resolve_bin_size
        c_bin_size = _resolve_bin_size(args.symbol)

    p_min = min(b.ohlc.l for b in bars)
    p_max = max(b.ohlc.h for b in bars)

    a = method_A(bars)
    b = method_B(bars)
    c = method_C(bars, c_bin_size)

    print()
    print(f"=== {args.symbol} {args.date}  ({len(bars)} bars, range {p_max - p_min:.2f}pt) ===")
    print()
    print(f"| Metric    | A: Sparse ladder    | B: Gaussian dense    | C: Current scipy (bin={c_bin_size})  |")
    print(f"|---        |---                  |---                   |---                                   |")
    print(f"| POC       | {_fmt_num(a['poc']):<19} | {_fmt_num(b['poc']):<20} | {_fmt_num(c['poc']):<36} |")
    print(f"| VAH       | {_fmt_num(a['vah']):<19} | {_fmt_num(b['vah']):<20} | {_fmt_num(c['vah']):<36} |")
    print(f"| VAL       | {_fmt_num(a['val']):<19} | {_fmt_num(b['val']):<20} | {_fmt_num(c['val']):<36} |")
    print(f"| Shape     | {a['shape']:<19} | {b['shape']:<20} | {c['shape']:<36} |")
    print(f"| HVN count | {len(a['hvn']):<19} | {len(b['hvn']):<20} | {len(c['hvn']):<36} |")
    print(f"| LVN count | {len(a['lvn']):<19} | {len(b['lvn']):<20} | {len(c['lvn']):<36} |")
    print()
    print(f"HVN A: {_fmt_zones(a['hvn'])}")
    print(f"HVN B: {_fmt_zones(b['hvn'])}")
    print(f"HVN C: {_fmt_zones(c['hvn'])}")
    print()
    print(f"LVN A: {_fmt_zones(a['lvn'])}")
    print(f"LVN B: {_fmt_zones(b['lvn'])}")
    print(f"LVN C: {_fmt_zones(c['lvn'])}")
    print()
    print(f"A: {a['info']}")
    print(f"B: {b['info']}")
    print(f"C: {c['info']}")

    # JSON dump
    out_dir = ROOT / "data" / "vp_compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.symbol}_{args.date}.json"
    out_path.write_text(json.dumps({
        "symbol": args.symbol, "date": args.date,
        "bars": len(bars), "range": p_max - p_min,
        "A": a, "B": b, "C": c,
    }, indent=2))
    print(f"\nJSON dump → {out_path}")


if __name__ == "__main__":
    main()
