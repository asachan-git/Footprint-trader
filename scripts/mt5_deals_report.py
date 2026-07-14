#!/usr/bin/env python3
"""mt5_deals_report.py — BROKER-REALIZED per-magic (setup × TF) P&L from MT5 deal history.

The broker-side ground truth (profit + swap + commission on each closed-out deal),
as opposed to scripts/magic_report.py which estimates from the server's own exec_emit
basket rows. Run the two side by side to see how far the estimate drifts from reality.

Requires the MT5 rpyc bridge running under wine (scripts/mt5_server.sh) — it queries
mt5.history_deals_get via execution.mt5_direct.get_client().

Usage:
  PYTHONPATH=. venv/bin/python scripts/mt5_deals_report.py --day 2026-07-13 [--symbol XAUUSD.pc]
  PYTHONPATH=. venv/bin/python scripts/mt5_deals_report.py --since 2026-07-13 --until 2026-07-14
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from execution.exec_bridge import MAGIC_BASE, _MAGIC_MAX, _STRAT_CODE, _CODE_TF, _TF_CODE
from execution.mt5_direct import get_client

_CODE_STRAT = {v: k for k, v in _STRAT_CODE.items()}
IST = timezone(timedelta(hours=5, minutes=30))


def _decode(magic: int) -> tuple[str, str]:
    if not magic or magic < MAGIC_BASE or magic >= _MAGIC_MAX:
        return ("?", "?")
    rel = int(magic) - MAGIC_BASE
    return (_CODE_STRAT.get(rel // 10, f"strat{rel // 10}"), _CODE_TF.get(rel % 10, f"tf{rel % 10}"))


def _stats(nets: list[float]) -> dict:
    wins = [v for v in nets if v > 0]
    gl = abs(sum(v for v in nets if v < 0))
    gw = sum(wins)
    return {"n": len(nets), "total": round(sum(nets), 2),
            "avg": round(sum(nets) / len(nets), 2) if nets else 0.0,
            "win": round(len(wins) / len(nets), 3) if nets else 0.0,
            "pf": round(gw / gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="single day YYYY-MM-DD (IST)")
    ap.add_argument("--since", default=None, help="from date YYYY-MM-DD (IST)")
    ap.add_argument("--until", default=None, help="to date YYYY-MM-DD (IST, exclusive)")
    ap.add_argument("--symbol", default="", help="broker symbol filter (e.g. XAUUSD.pc)")
    ap.add_argument("--host", default=None); ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    if args.day:
        d = datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=IST)
        ts_from, ts_to = d.timestamp(), d.timestamp() + 86400
    else:
        if not (args.since and args.until):
            ap.error("give --day OR both --since and --until")
        ts_from = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=IST).timestamp()
        ts_to = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=IST).timestamp()

    kw = {}
    if args.host: kw["host"] = args.host
    if args.port: kw["port"] = args.port
    try:
        cli = get_client(**kw)
        if not cli.healthy():
            raise RuntimeError("bridge unhealthy")
        deals = cli.history_deals(ts_from, ts_to, symbol=args.symbol)
    except Exception as e:
        print(f"MT5 bridge NOT reachable ({e}) — start it under wine: bash scripts/mt5_server.sh")
        return
    label = args.day or f"{args.since}..{args.until}"
    print(f"\nBROKER-REALIZED deals — {label} IST  (symbol={args.symbol or 'all'})")
    print("=" * 78)
    if not deals:
        print("no closed-out deals in window (or all filtered out).\n"); return

    by_magic: dict[int, list[float]] = defaultdict(list)
    non_ours = 0.0
    for d in deals:
        mg = int(d.get("magic") or 0)
        by_magic[mg].append(float(d.get("net", 0.0)))
        if _decode(mg) == ("?", "?"):
            non_ours += float(d.get("net", 0.0))

    hdr = f"{'setup':18}{'TF':>4}{'magic':>8}{'n':>4}{'total$':>10}{'avg$':>8}{'win%':>6}{'PF':>7}"
    print(hdr); print("-" * len(hdr))
    setup_roll: dict[str, list[float]] = defaultdict(list)
    tf_roll: dict[str, list[float]] = defaultdict(list)
    alln: list[float] = []
    for mg in sorted(by_magic, key=lambda m: _decode(m)):
        setup, tf = _decode(mg)
        s = _stats(by_magic[mg])
        print(f"{setup:18}{tf:>4}{mg:>8}{s['n']:>4}{s['total']:>10.2f}{s['avg']:>8.2f}"
              f"{s['win']*100:>5.1f}%{s['pf']:>7}")
        setup_roll[setup] += by_magic[mg]; tf_roll[tf] += by_magic[mg]; alln += by_magic[mg]

    print("\nBy setup:")
    for setup in sorted(setup_roll):
        s = _stats(setup_roll[setup])
        print(f"  {setup:18} n={s['n']:>3}  total={s['total']:>9.2f}  avg={s['avg']:>7.2f}  "
              f"win={s['win']*100:>4.1f}%  PF={s['pf']}")
    print("\nBy TF:")
    for tf in sorted(tf_roll, key=lambda t: _TF_CODE.get(t, 99)):
        s = _stats(tf_roll[tf])
        print(f"  {tf:>4}  n={s['n']:>3}  total={s['total']:>9.2f}  avg={s['avg']:>7.2f}  "
              f"win={s['win']*100:>4.1f}%  PF={s['pf']}")
    o = _stats(alln)
    print(f"\nOVERALL  n={o['n']}  total=${o['total']}  avg=${o['avg']}  win={o['win']*100:.1f}%  PF={o['pf']}")
    if non_ours:
        print(f"(note: ${round(non_ours,2)} of the total is on non-FB / '?' magics — manual trades or other EAs)")
    print()


if __name__ == "__main__":
    main()
