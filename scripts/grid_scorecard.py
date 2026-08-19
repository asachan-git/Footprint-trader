#!/usr/bin/env python3
"""grid_scorecard.py — the merge gate for grid changes.

Scores a run on the three things that separated the profitable week from the
blown account, per (setup x timeframe):

  one-sided rate  fraction of grids that filled ONE side only. A grid whose
                  level was read correctly resolves one way and leaves the
                  other ladder resting. A grid that fills both sides bought
                  the high and sold the low and now needs a much larger move
                  just to get back to flat.
  USC per lot     edge per unit of volume traded. The only scale-free P&L
                  measure here: raw net-$ and win rate both rose while the
                  edge collapsed. Commission is ~3 USC/lot/side, so this is
                  measured against a hard floor.
  opposing depth  how deep the losing side got before the cycle closed.

Two input sources, because the durable cycle log only starts 2026-07-20:

  cycles   data/cycles/cycle_outcomes_YYYY-MM-DD.jsonl  (server-side truth;
           carries buy_n/sell_n/buys/sells/pnl_at_exit/trough/exit_reason)
  broker   a CSV derived from an MT5 statement, one row per grid, columns:
           date,time,setup,tf,placed_b,filled_b,placed_s,filled_s,lots,net
           (this is the only source that can supply lots, hence USC/lot)

Exit status is 1 when any active threshold fails, so this can gate a merge.

Usage
  scripts/grid_scorecard.py --cycles data/cycles                 # all dates
  scripts/grid_scorecard.py --cycles data/cycles --date 2026-08-03 --magic 774000
  scripts/grid_scorecard.py --broker june_grids.csv
  scripts/grid_scorecard.py --cycles data/cycles --baseline scripts/baselines/grid_baseline.json
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict

# Thresholds. Both derived from the 22-26 June reference run; see baselines.
MIN_ONE_SIDED_PCT = 60.0     # June aggregate was 68.2
MIN_USC_PER_LOT = 10.0       # June aggregate was +51.7; commission floor is -3.0/lot/side


def _shape(fb: int, fs: int) -> str:
    if not fb and not fs:
        return "no_fill"
    if fb and fs:
        return "both"
    return "one_sided"


def load_cycles(root: str, only_date: str | None, magic_base: int | None = None) -> list[dict]:
    """Read the server's durable per-cycle outcome log.

    magic_base filters to one branch's magic decade (e.g. 770000 or 774000) —
    two branches trading the same day under different bases is the normal A/B
    shape, and pooling them scores neither."""
    out: list[dict] = []
    pat = f"cycle_outcomes_{only_date}.jsonl" if only_date else "cycle_outcomes_*.jsonl"
    for path in sorted(glob.glob(os.path.join(root, pat))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(r.get("account", "")).startswith("test-"):
                    continue          # synthetic harness rows
                if magic_base is not None:
                    m = int(r.get("magic") or 0)
                    if not (magic_base <= m < magic_base + 1000):
                        continue
                fb, fs = int(r.get("buys") or 0), int(r.get("sells") or 0)
                out.append({
                    "date": r.get("date", ""),
                    "setup": r.get("trigger_kind") or "?",
                    "tf": r.get("tf") or "-",
                    "placed_b": int(r.get("buy_n") or 0),
                    "placed_s": int(r.get("sell_n") or 0),
                    "filled_b": fb,
                    "filled_s": fs,
                    "shape": _shape(fb, fs),
                    "opp": min(fb, fs),
                    "net": float(r.get("pnl_at_exit") or 0.0),
                    "lots": None,
                    "trough": r.get("trough"),
                    "exit_reason": r.get("exit_reason") or "",
                })
    return out


def load_broker(path: str) -> list[dict]:
    """Read a per-grid CSV derived from an MT5 statement."""
    out: list[dict] = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            fb, fs = int(r.get("filled_b") or 0), int(r.get("filled_s") or 0)
            net = r.get("net_usc") or r.get("net") or ""
            lots_b = float(r.get("lots_b") or 0)
            lots_s = float(r.get("lots_s") or 0)
            out.append({
                "date": r.get("date", ""),
                "setup": r.get("setup") or "?",
                "tf": r.get("tf") or "-",
                "placed_b": int(r.get("placed_b") or 0),
                "placed_s": int(r.get("placed_s") or 0),
                "filled_b": fb,
                "filled_s": fs,
                "shape": r.get("shape") or _shape(fb, fs),
                "opp": int(r.get("opp_fills") or min(fb, fs)),
                "net": float(net) if net not in ("", None) else None,
                "lots": (lots_b + lots_s) or None,
                "trough": None,
                "exit_reason": r.get("exit_reason") or "",
            })
    return out


def score(rows: list[dict]) -> dict:
    """Aggregate overall and per (setup, tf)."""
    def agg(rs: list[dict]) -> dict:
        live = [r for r in rs if r["shape"] != "no_fill"]
        one = sum(1 for r in live if r["shape"] == "one_sided")
        both = sum(1 for r in live if r["shape"] == "both")
        with_pnl = [r for r in rs if r["net"] is not None]
        with_lots = [r for r in rs if r["lots"]]
        net = sum(r["net"] for r in with_pnl)
        lots = sum(r["lots"] for r in with_lots)
        troughs = [float(r["trough"]) for r in rs if r["trough"] is not None]
        return {
            "grids": len(rs),
            "no_fill": len(rs) - len(live),
            "one_sided": one,
            "both": both,
            "one_sided_pct": (100.0 * one / len(live)) if live else None,
            "net": net if with_pnl else None,
            "lots": lots if with_lots else None,
            "usc_per_lot": (net / lots) if (with_lots and lots) else None,
            "pnl_per_grid": (net / len(with_pnl)) if with_pnl else None,
            "median_trough": st.median(troughs) if troughs else None,
            "opp_depth": dict(sorted(
                ((k, v) for k, v in _count(r["opp"] for r in live).items()))),
            "exits": dict(sorted(_count(r["exit_reason"] for r in rs if r["exit_reason"]).items(),
                                 key=lambda kv: -kv[1])),
        }

    by = defaultdict(list)
    for r in rows:
        by[(r["setup"], r["tf"])].append(r)
    return {
        "overall": agg(rows),
        "by_setup_tf": {f"{k[0]}|{k[1]}": agg(v)
                        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))},
    }


def _count(it) -> dict:
    d: dict = {}
    for x in it:
        d[x] = d.get(x, 0) + 1
    return d


def _fmt(x, nd=1, dash="—"):
    return dash if x is None else f"{x:,.{nd}f}"


def report(res: dict, baseline: dict | None) -> bool:
    ok = True
    o = res["overall"]
    print(f"\n{'setup | tf':26}{'grids':>7}{'nofill':>7}{'1-sided':>8}{'both':>6}"
          f"{'1-sided%':>10}{'net':>11}{'/lot':>9}{'/grid':>9}")
    for name, a in list(res["by_setup_tf"].items()):
        print(f"{name:26}{a['grids']:7d}{a['no_fill']:7d}{a['one_sided']:8d}{a['both']:6d}"
              f"{_fmt(a['one_sided_pct']):>10}{_fmt(a['net'], 0):>11}"
              f"{_fmt(a['usc_per_lot']):>9}{_fmt(a['pnl_per_grid'], 0):>9}")
    print(f"{'OVERALL':26}{o['grids']:7d}{o['no_fill']:7d}{o['one_sided']:8d}{o['both']:6d}"
          f"{_fmt(o['one_sided_pct']):>10}{_fmt(o['net'], 0):>11}"
          f"{_fmt(o['usc_per_lot']):>9}{_fmt(o['pnl_per_grid'], 0):>9}")

    if o["opp_depth"]:
        print("\nopposing legs filled → grids: " +
              "  ".join(f"{k}:{v}" for k, v in o["opp_depth"].items()))
    if o["exits"]:
        print("exit reasons: " + "  ".join(f"{k}={v}" for k, v in o["exits"].items()))
    if o["median_trough"] is not None:
        print(f"median trough: {o['median_trough']:,.0f}")

    print("\n── gate ──")
    if o["one_sided_pct"] is None:
        print("  one-sided rate  : no filled grids — INCONCLUSIVE")
    elif o["one_sided_pct"] < MIN_ONE_SIDED_PCT:
        ok = False
        print(f"  one-sided rate  : {o['one_sided_pct']:.1f}%  FAIL  (min {MIN_ONE_SIDED_PCT:.0f}%)")
    else:
        print(f"  one-sided rate  : {o['one_sided_pct']:.1f}%  pass  (min {MIN_ONE_SIDED_PCT:.0f}%)")

    if o["usc_per_lot"] is None:
        print("  USC per lot     : not available from this source (needs --broker)")
    elif o["usc_per_lot"] < MIN_USC_PER_LOT:
        ok = False
        print(f"  USC per lot     : {o['usc_per_lot']:.1f}   FAIL  (min {MIN_USC_PER_LOT:.0f})")
    else:
        print(f"  USC per lot     : {o['usc_per_lot']:.1f}   pass  (min {MIN_USC_PER_LOT:.0f})")

    if baseline:
        print("\n── vs reference runs ──")
        for name, ref in baseline.get("runs", {}).items():
            bits = [f"1-sided {ref['one_sided_pct']:.0f}%"]
            if ref.get("usc_per_lot") is not None:
                bits.append(f"{ref['usc_per_lot']:+.1f}/lot")
            print(f"  {name:22} {'  '.join(bits):24} {ref.get('note', '')}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a grid run; exit 1 on gate failure.")
    ap.add_argument("--cycles", help="directory of cycle_outcomes_*.jsonl")
    ap.add_argument("--broker", help="per-grid CSV derived from an MT5 statement")
    ap.add_argument("--date", help="restrict --cycles to one YYYY-MM-DD")
    ap.add_argument("--magic", type=int, help="restrict --cycles to one magic base, e.g. 774000")
    ap.add_argument("--baseline", default="scripts/baselines/grid_baseline.json")
    ap.add_argument("--json", action="store_true", help="emit the scorecard as JSON")
    a = ap.parse_args()

    rows: list[dict] = []
    if a.cycles:
        rows += load_cycles(a.cycles, a.date, a.magic)
    if a.broker:
        rows += load_broker(a.broker)
    if not rows:
        print("no rows — pass --cycles and/or --broker", file=sys.stderr)
        return 2

    res = score(rows)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return 0

    baseline = None
    if a.baseline and os.path.exists(a.baseline):
        with open(a.baseline) as fh:
            baseline = json.load(fh)
    return 0 if report(res, baseline) else 1


if __name__ == "__main__":
    raise SystemExit(main())
