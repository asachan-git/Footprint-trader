#!/usr/bin/env python3
"""magic_report.py — per-magic (setup × TF) P&L from the grid audit.

Buckets grid-cycle EXITS in data/exec_emit.jsonl by their magic, decoded into
(setup, TF) via the live maps in execution.exec_bridge. Setups run in parallel and
are magic-isolated (P1/P2), so this is the by-magic review:

  setup        TF    n   total$    avg$   win%    PF   reasons
  hvn_inside…  15m   12   +8.40   +0.70  58.3%  1.9   {net_target:7, full_hedge:5}

Outcome per cycle = the flatten row's `pnl` (basket $ at CLOSE_ALL — the system's
own ground truth; MT5 History grouped by magic is the broker-side truth). bias_book
partial-books are counted separately (mid-cycle, not a full exit).

Usage:
  PYTHONPATH=. venv/bin/python scripts/magic_report.py [--symbol BTCUSD] [--since 2026-06-21]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from execution.exec_bridge import MAGIC_BASE, _STRAT_CODE, _CODE_TF, _TF_CODE  # noqa: live maps

LOG = Path(__file__).resolve().parent.parent / "data" / "exec_emit.jsonl"
_CODE_STRAT = {v: k for k, v in _STRAT_CODE.items()}
FLATTEN = {"net_target", "full_hedge", "leg_tp", "leg_closed_other"}  # full-cycle closes


def _decode(magic: int) -> tuple[str, str]:
    """magic → (setup_name, tf). MAGIC_BASE + strat·10 + tf_code."""
    if not magic or magic < MAGIC_BASE or magic >= MAGIC_BASE + 100:
        return ("?", "?")
    rel = int(magic) - MAGIC_BASE
    setup = _CODE_STRAT.get(rel // 10, f"strat{rel // 10}")
    tf = _CODE_TF.get(rel % 10, f"tf{rel % 10}")
    return (setup, tf)


def _stats(pnls: list[float], reasons: list[str]) -> dict:
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v < 0]
    gw, gl = sum(wins), abs(sum(losses))
    return {
        "n": len(pnls),
        "total": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "win": round(len(wins) / len(pnls), 3) if pnls else 0.0,
        "pf": round(gw / gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0),
        "reasons": dict(sorted(((r, reasons.count(r)) for r in set(reasons)), key=lambda x: -x[1])),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="broker symbol filter (e.g. BTCUSD)")
    ap.add_argument("--since", default=None, help="rows on/after this date (YYYY-MM-DD, local)")
    args = ap.parse_args()
    since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp() if args.since else 0.0

    by_magic: dict[int, list[float]] = defaultdict(list)
    by_magic_reasons: dict[int, list[str]] = defaultdict(list)
    partials: dict[int, int] = defaultdict(int)   # bias_book mid-cycle books
    if not LOG.exists():
        print("no data/exec_emit.jsonl yet"); return

    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ts", 0) < since_ts:
            continue
        if args.symbol and r.get("broker_symbol") not in (args.symbol, None):
            continue
        reason = r.get("exit_reason")
        mg = int(r.get("magic") or 0)
        if reason == "bias_book_trail":
            partials[mg] += 1
        elif reason in FLATTEN and r.get("pnl") is not None and mg:
            by_magic[mg].append(float(r["pnl"]))
            by_magic_reasons[mg].append(reason)

    print(f"\nPer-magic grid report — exec_emit.jsonl  (symbol={args.symbol or 'all'} "
          f"since={args.since or 'all'})")
    print("=" * 86)
    if not by_magic:
        print("No cycle exits recorded yet — let the setups run, then re-run.\n"); return

    hdr = f"{'setup':18}{'TF':>4}{'magic':>8}{'n':>4}{'total$':>9}{'avg$':>8}{'win%':>6}{'PF':>6}   reasons"
    print(hdr); print("-" * len(hdr))
    setup_roll: dict[str, list[float]] = defaultdict(list)
    tf_roll: dict[str, list[float]] = defaultdict(list)
    allp: list[float] = []
    for mg in sorted(by_magic, key=lambda m: _decode(m)):
        setup, tf = _decode(mg)
        s = _stats(by_magic[mg], by_magic_reasons[mg])
        pstr = f"  +{partials[mg]}book" if partials.get(mg) else ""
        print(f"{setup:18}{tf:>4}{mg:>8}{s['n']:>4}{s['total']:>9.2f}{s['avg']:>8.3f}"
              f"{s['win']*100:>5.1f}%{s['pf']:>6}   {s['reasons']}{pstr}")
        setup_roll[setup] += by_magic[mg]
        tf_roll[tf] += by_magic[mg]
        allp += by_magic[mg]

    print("\nBy setup (all TFs):")
    for setup in sorted(setup_roll):
        s = _stats(setup_roll[setup], [])
        print(f"  {setup:18} n={s['n']:>3}  total={s['total']:>8.2f}  avg={s['avg']:>7.3f}  "
              f"win={s['win']*100:>4.1f}%  PF={s['pf']}")
    print("\nBy TF (all setups):")
    for tf in sorted(tf_roll, key=lambda t: _TF_CODE.get(t, 99)):
        s = _stats(tf_roll[tf], [])
        print(f"  {tf:>4}  n={s['n']:>3}  total={s['total']:>8.2f}  avg={s['avg']:>7.3f}  "
              f"win={s['win']*100:>4.1f}%  PF={s['pf']}")
    o = _stats(allp, [])
    print(f"\nOVERALL  n={o['n']}  total=${o['total']}  avg=${o['avg']}  win={o['win']*100:.1f}%  PF={o['pf']}\n")


if __name__ == "__main__":
    main()
