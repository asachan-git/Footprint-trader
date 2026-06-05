#!/usr/bin/env python3
"""VP Consistency Check — cached daily VP HVN/LVN vs rolling N-bar VP at the
same wall-clock time.

Different consumers in the codebase read different VPs:
  - republic, reversal_si:       vp_cache.get(daily)        (cached daily IST)
  - reversal, coup, reversal_choch, wave_fib, reversal_pattern, cvd_sweep_study:
                                 vp_compute(bars[i-N:i+1])   (rolling N-bar)
  - dashboard drawSessionVP:     profileFromSlice (percentile 80/20, visual)

These are NOT the same VP. This script quantifies the drift between cached
daily and rolling N-bar at the END of each cached session. Output:

  data/reports/vp_consistency.md   side-by-side HVN/LVN table per session

For each cached daily snapshot:
  - find the bar whose close_ts is at the session end (latest bar inside the
    session's [start_ts, end_ts] window)
  - compute rolling VP using bars[i-N+1:i+1] (N = 96 for 15m, 288 for 5m)
  - print:
      cached:  HVN count, LVN count, POC, VAH, VAL
      rolling: same
      drift:   ΔPOC in ATR, HVN Jaccard, LVN Jaccard

Usage:
  .venv/bin/python scripts/vp_consistency_check.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from pipeline.state_store import _deserialize
from pipeline.features.atr import atr
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
from pipeline.features import vp_cache as vp_cache_mod
from pipeline.features.vp_cache import _day_bounds

FP_DIR    = ROOT / "data" / "footprint"
CACHE_PATH = ROOT / "data" / "vp_cache.json"
REPORT    = ROOT / "data" / "reports" / "vp_consistency.md"

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TFS = ["15m"]   # cache is per-day so 15m is the meaningful comparison

VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}


def load_bars(symbol: str, tf: str) -> list:
    seen: dict[int, object] = {}
    for f in sorted(FP_DIR.glob(f"{symbol}_{tf}*.jsonl")):
        if ".bak" in f.name:
            continue
        for line in f.open():
            if not line.strip():
                continue
            try:
                b = _deserialize(line)
            except Exception:
                continue
            if b.symbol == symbol and b.tf == tf:
                seen[b.close_ts] = b
    return [seen[k] for k in sorted(seen)]


def _zone_jaccard(a: list[dict], b: list[dict]) -> float:
    """Total-overlap / total-union on lists of {low, high} intervals.

    Treats both sides as multisets of price-intervals. Returns 0 if both empty.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    # build merged union range
    def total_len(zones):
        return sum(z["high"] - z["low"] for z in zones)
    # intersection: pair every a with every b
    inter = 0.0
    for za in a:
        for zb in b:
            lo = max(za["low"], zb["low"])
            hi = min(za["high"], zb["high"])
            if hi > lo:
                inter += (hi - lo)
    union = total_len(a) + total_len(b) - inter
    return inter / union if union > 0 else 0.0


def _summary_zones(zones: list[dict]) -> str:
    if not zones:
        return "—"
    return ", ".join(f"[{z['low']:.2f}–{z['high']:.2f}]" for z in zones)


def scan(symbol: str, tf: str, cache: dict) -> list[dict]:
    bars = load_bars(symbol, tf)
    if not bars:
        return []
    win = VP_WIN.get(tf, 96)
    bin_size = DEFAULT_BIN_SIZE.get(symbol)
    sym_cache = cache.get(symbol, {})
    daily = sym_cache.get("daily", {})
    offset = sym_cache.get("venue_price_offset", 0.0) or 0.0
    anchor = sym_cache.get("session_start_utc", 0)

    # index bars by close_ts for fast lookup
    ts_to_idx = {b.close_ts: i for i, b in enumerate(bars)}
    sorted_ts = sorted(ts_to_idx.keys())

    rows: list[dict] = []
    for day_key, snap in sorted(daily.items()):
        try:
            _, end_ts = _day_bounds(day_key, anchor)
        except Exception:
            continue
        if not end_ts:
            continue
        # find the latest bar with close_ts <= end_ts
        idx = None
        for ts in sorted_ts:
            if ts <= end_ts:
                idx = ts_to_idx[ts]
            else:
                break
        if idx is None or idx < win:
            continue
        i = idx

        # cached zones (un-shift the venue offset to compare with raw bars)
        cached_hvn = [{"low": z["low"] - offset, "high": z["high"] - offset}
                      for z in (snap.get("hvn_zones") or [])]
        cached_lvn = [{"low": z["low"] - offset, "high": z["high"] - offset}
                      for z in (snap.get("lvn_zones") or [])]
        cached_poc = (snap.get("poc") - offset) if snap.get("poc") is not None else None
        cached_vah = (snap.get("vah") - offset) if snap.get("vah") is not None else None
        cached_val = (snap.get("val") - offset) if snap.get("val") is not None else None

        # rolling VP at this bar
        rolling = vp_compute(
            bars[i - win + 1:i + 1], "daily", bars[i].ohlc.c, bin_size=bin_size
        )

        atr_val = atr(bars[max(0, i - 50):i + 1]) or 0.0
        poc_drift_atr = (
            abs((cached_poc or 0) - (rolling.poc or 0)) / atr_val if atr_val > 0 else None
        )

        rows.append({
            "symbol": symbol, "tf": tf, "day": day_key, "end_ts": end_ts,
            "atr": round(atr_val, 4),
            "cached_hvn_n": len(cached_hvn),
            "cached_lvn_n": len(cached_lvn),
            "rolling_hvn_n": len(rolling.hvn_zones or []),
            "rolling_lvn_n": len(rolling.lvn_zones or []),
            "cached_poc": cached_poc, "rolling_poc": rolling.poc,
            "cached_vah": cached_vah, "rolling_vah": rolling.vah,
            "cached_val": cached_val, "rolling_val": rolling.val,
            "poc_drift_atr": round(poc_drift_atr, 2) if poc_drift_atr is not None else None,
            "hvn_jaccard": round(_zone_jaccard(cached_hvn, rolling.hvn_zones or []), 3),
            "lvn_jaccard": round(_zone_jaccard(cached_lvn, rolling.lvn_zones or []), 3),
            "cached_hvn_str": _summary_zones(cached_hvn),
            "rolling_hvn_str": _summary_zones(rolling.hvn_zones or []),
            "cached_lvn_str": _summary_zones(cached_lvn),
            "rolling_lvn_str": _summary_zones(rolling.lvn_zones or []),
        })
    return rows


def build_report(all_rows: list[dict]) -> str:
    out: list[str] = [
        "# VP Consistency Check — cached daily vs rolling N-bar",
        "",
        "For each cached daily VP snapshot, compares the cached zones against",
        "the rolling N-bar VP at the same end-of-session bar. Drift here means",
        "two strategies reading 'the same' HVN at the same wall-clock moment",
        "will see DIFFERENT zones.",
        "",
        "  cached  = vp_cache.json (IST session day, full-day window)",
        f"  rolling = vp_compute(bars[i-N+1:i+1]) with N=VP_WIN[{', '.join(f'{tf}={n}' for tf, n in VP_WIN.items())}]",
        "",
        "Jaccard = total interval overlap / total interval union (1.0 = identical zones, 0 = no overlap).",
        "ΔPOC in ATR = |cached_poc − rolling_poc| / ATR(14).",
        "",
    ]

    # summary table
    out += [
        "## Per-snapshot drift",
        "",
        "| symbol | tf | day | cHVN | rHVN | cLVN | rLVN | HVN Jacc | LVN Jacc | ΔPOC ATR |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in all_rows:
        out.append(
            f"| {r['symbol']} | {r['tf']} | {r['day']} | "
            f"{r['cached_hvn_n']} | {r['rolling_hvn_n']} | "
            f"{r['cached_lvn_n']} | {r['rolling_lvn_n']} | "
            f"{r['hvn_jaccard']:.3f} | {r['lvn_jaccard']:.3f} | "
            f"{r['poc_drift_atr'] if r['poc_drift_atr'] is not None else '—'} |"
        )

    # aggregates
    if all_rows:
        out += [
            "",
            "## Aggregates",
            "",
            "| symbol | tf | n | median HVN Jacc | median LVN Jacc | median ΔPOC ATR | "
            "med cHVN | med rHVN | med cLVN | med rLVN |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        by_key: dict[tuple[str, str], list[dict]] = {}
        for r in all_rows:
            by_key.setdefault((r["symbol"], r["tf"]), []).append(r)
        for (sym, tf), rows in by_key.items():
            jh = statistics.median(r["hvn_jaccard"] for r in rows)
            jl = statistics.median(r["lvn_jaccard"] for r in rows)
            pd = [r["poc_drift_atr"] for r in rows if r["poc_drift_atr"] is not None]
            mpd = statistics.median(pd) if pd else None
            mch = statistics.median(r["cached_hvn_n"] for r in rows)
            mrh = statistics.median(r["rolling_hvn_n"] for r in rows)
            mcl = statistics.median(r["cached_lvn_n"] for r in rows)
            mrl = statistics.median(r["rolling_lvn_n"] for r in rows)
            out.append(
                f"| {sym} | {tf} | {len(rows)} | {jh:.3f} | {jl:.3f} | "
                f"{mpd if mpd is not None else '—'} | {mch} | {mrh} | {mcl} | {mrl} |"
            )

    # per-snapshot side-by-side
    out.append("\n## Side-by-side per snapshot (HVN + LVN)\n")
    for r in all_rows:
        out.append(
            f"\n### {r['symbol']} {r['tf']} — {r['day']}  "
            f"(atr={r['atr']}, ΔPOC={r['poc_drift_atr']} ATR, "
            f"HVN Jacc={r['hvn_jaccard']:.3f}, LVN Jacc={r['lvn_jaccard']:.3f})"
        )
        out.append(f"- cached  POC={r['cached_poc']}  VAH={r['cached_vah']}  VAL={r['cached_val']}")
        out.append(f"- rolling POC={r['rolling_poc']}  VAH={r['rolling_vah']}  VAL={r['rolling_val']}")
        out.append(f"- cached  HVN  ({r['cached_hvn_n']}): {r['cached_hvn_str']}")
        out.append(f"- rolling HVN  ({r['rolling_hvn_n']}): {r['rolling_hvn_str']}")
        out.append(f"- cached  LVN  ({r['cached_lvn_n']}): {r['cached_lvn_str']}")
        out.append(f"- rolling LVN  ({r['rolling_lvn_n']}): {r['rolling_lvn_str']}")

    return "\n".join(out) + "\n"


def main():
    if not CACHE_PATH.exists():
        print(f"no cache at {CACHE_PATH}")
        return
    with CACHE_PATH.open() as f:
        cache = json.load(f)

    all_rows: list[dict] = []
    for sym in SYMBOLS:
        for tf in TFS:
            rows = scan(sym, tf, cache)
            all_rows.extend(rows)
            print(f"  {sym} {tf}: {len(rows)} cached snapshots compared")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(build_report(all_rows))
    print(f"\nreport → {REPORT}")


if __name__ == "__main__":
    main()
