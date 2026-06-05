#!/usr/bin/env python3
"""Phantom-fill audit — did recorded paper entries actually trade in the market?

A "phantom fill" = a position logged as filled at an `entry` price the market never
traded at the fill bar (e.g. a limit anchor booked at its zone level before price
reached it — the bug fixed in 131b87e). For every open record in each strategy's
data/strategies/<name>/positions.jsonl we load the REAL footprint bar named by its
`bar_id` and check entry ∈ [low, high] (± a tiny tick epsilon).

For an out-of-range entry we scan FORWARD up to LOOKAHEAD bars to tell apart:
  - premature   : never touched in the fill bar, but a later bar DID touch it → the
                  fill was booked too early (timing phantom; PnL clock starts wrong).
  - never_touch : price never reached the entry within the window → hard phantom.
Direction-correctness is also checked: a long should fill at/above the low it dipped
to, a short at/below the high — i.e. the touch must be reachable, which [low,high] ⊇
entry already guarantees.

Usage: .venv/bin/python scripts/check_phantom_fills.py [strategy ...]
       (no args → every store under data/strategies/)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from scripts.reversal_study import load_bars

STORES = ROOT / "data" / "strategies"
LOOKAHEAD = 24                 # bars to scan forward for a real touch
TICK_EPS = {"BTCUSDT": 0.5, "XAUTUSDT": 0.05}   # ½-tick slack for float/rounding


def _eps(symbol: str, price: float) -> float:
    return max(TICK_EPS.get(symbol, 0.0), abs(price) * 1e-6)


def _bar_index(symbol: str, tf: str, cache: dict) -> tuple[dict, list]:
    key = (symbol, tf)
    if key not in cache:
        bars = load_bars(symbol, tf)
        cache[key] = ({b.bar_id: i for i, b in enumerate(bars)}, bars)
    return cache[key]


def audit_store(name: str, cache: dict) -> dict:
    pos_file = STORES / name / "positions.jsonl"
    if not pos_file.exists():
        return {}
    rows = []
    for line in pos_file.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") == "open":
            rows.append(r)

    res = {"n": 0, "ok": 0, "premature": [], "never_touch": [], "no_bar": []}
    for r in rows:
        sym, tf, bar_id = r.get("symbol"), r.get("tf"), r.get("bar_id")
        entry = r.get("entry")
        pid = r.get("position_id", "?")
        if entry is None or not bar_id:
            continue
        res["n"] += 1
        idx_map, bars = _bar_index(sym, tf, cache)
        i = idx_map.get(bar_id)
        if i is None:
            res["no_bar"].append((pid, bar_id))
            continue
        eps = _eps(sym, entry)
        b = bars[i]
        if (b.ohlc.l - eps) <= entry <= (b.ohlc.h + eps):
            res["ok"] += 1
            continue
        # out of the fill bar's range → look for a later real touch
        touch_k = None
        for k in range(1, LOOKAHEAD + 1):
            if i + k >= len(bars):
                break
            bb = bars[i + k]
            if (bb.ohlc.l - eps) <= entry <= (bb.ohlc.h + eps):
                touch_k = k
                break
        rec = (pid, r.get("side"), entry, round(b.ohlc.l, 4), round(b.ohlc.h, 4),
               r.get("ts_ist", ""), touch_k)
        (res["premature"] if touch_k else res["never_touch"]).append(rec)
    return res


def main():
    names = sys.argv[1:] or sorted(p.name for p in STORES.iterdir() if p.is_dir())
    cache: dict = {}
    grand = {"n": 0, "ok": 0, "prem": 0, "never": 0, "nobar": 0}
    flagged = []

    for name in names:
        res = audit_store(name, cache)
        if not res or res["n"] == 0:
            continue
        prem, never, nobar = len(res["premature"]), len(res["never_touch"]), len(res["no_bar"])
        grand["n"] += res["n"]; grand["ok"] += res["ok"]
        grand["prem"] += prem; grand["never"] += never; grand["nobar"] += nobar
        status = "OK" if (prem == never == 0) else "⚠ PHANTOM"
        print(f"{status:11s} {name:18s} n={res['n']:3d} in-range={res['ok']:3d} "
              f"premature={prem} never_touched={never} no_bar={nobar}")
        if prem or never:
            flagged.append((name, res))

    print(f"\nTOTAL  positions={grand['n']}  in-range={grand['ok']}  "
          f"premature={grand['prem']}  never_touched={grand['never']}  "
          f"no_bar(uncheckable)={grand['nobar']}")

    for name, res in flagged:
        print(f"\n── {name}: out-of-range fills ──")
        for tag, recs in (("NEVER TOUCHED", res["never_touch"]), ("PREMATURE", res["premature"])):
            for pid, side, entry, lo, hi, ts, tk in recs:
                tail = "never touched in window" if tag == "NEVER TOUCHED" else f"touched +{tk} bars later"
                print(f"  [{tag}] {pid} {side} entry={entry} fill-bar[{lo},{hi}] {ts} — {tail}")

    if grand["prem"] == grand["never"] == 0:
        print("\n✅ No phantom fills: every checkable entry traded within its fill bar.")
    else:
        print(f"\n⚠ {grand['prem']+grand['never']} suspicious fills — see above.")


if __name__ == "__main__":
    main()
