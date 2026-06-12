#!/usr/bin/env python3
"""CVD-div gate A/B report — each gated twin vs its ungated origin, MATCHED window.

The gated twins (*_cvd) deployed mid-run, so we compare only trades CLOSED since
the twins started (anchor = earliest twin open across the stores). Reports per-pair
n / WR / sumR / avgR for origin vs twin, and flags when each twin has ≥ READY_N
closed trades (enough to start reading the A/B).

Usage:  .venv/bin/python scripts/cvd_ab_report.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAIRS = [("democracy", "democracy_cvd"),
         ("congress_start", "congress_start_cvd"),
         ("wave_fib", "wave_fib_cvd"),
         ("reversal_si", "reversal_si_cvd")]
READY_N = 25


def _trades(name):
    """Return (opens_by_id, [(close_ts, realized_r, symbol)]) for a strategy store."""
    p = ROOT / "data" / "strategies" / name / "positions.jsonl"
    opens, closed = {}, []
    if not p.exists():
        return opens, closed
    for line in p.open():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") == "open":
            opens[e["position_id"]] = e
        elif e.get("type") == "close" and e.get("realized_r") is not None:
            sym = opens.get(e["position_id"], {}).get("symbol", "?")
            closed.append((e.get("ts", 0), float(e["realized_r"]), sym))
    return opens, closed


def main():
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M IST", time.gmtime(now + 19800))
    # anchor = earliest twin open (≈ deploy); fall back to "now" if no twin trades yet
    twin_opens = []
    for _, tw in PAIRS:
        op, _ = _trades(tw)
        twin_opens += [e.get("ts", 0) for e in op.values() if e.get("ts")]
    anchor = min(twin_opens) if twin_opens else now
    age_h = (now - anchor) / 3600

    def stat(name, symbol):
        _, closed = _trades(name)
        c = [r for ts, r, s in closed if ts >= anchor and (symbol is None or s == symbol)]
        if not c:
            return "n=0"
        w = sum(1 for r in c if r > 0)
        return f"n={len(c):<3} WR={100*w/len(c):>3.0f}% sumR={sum(c):>7.2f} avgR={sum(c)/len(c):+.3f}"

    # gate is BTC-good / XAUT-bad (cross-symbol re-val) → split by symbol, never aggregate
    lines = [f"=== CVD-div gate A/B  [{stamp}]  (matched: last {age_h:.1f}h since twin deploy) ==="]
    ready_all = True
    for origin, twin in PAIRS:
        _, tc = _trades(twin)
        twin_n = len([1 for ts, _, _ in tc if ts >= anchor])
        ready = twin_n >= READY_N
        ready_all &= ready
        lines.append(f"\n{origin}  ({'READY' if ready else f'{twin_n}/{READY_N} twin trades'})")
        lines.append(f"  {'symbol':<9}{'A (ungated)':<34}{'B (gated _cvd)':<34}")
        for symbol in ("BTCUSDT", "XAUTUSDT"):
            a, b = stat(origin, symbol), stat(twin, symbol)
            if a == "n=0" and b == "n=0":
                continue
            lines.append(f"  {symbol:<9}{a:<34}{b:<34}")
    lines.append("")
    lines.append("READABLE — all twins ≥25 trades; per symbol, does B beat A on avgR/sumR? "
                 "(expect: gate helps BTC, NOT XAUT — vote loses on XAUT even gated)"
                 if ready_all else
                 "Still maturing — twins need ≥25 closed trades each before the A/B is meaningful.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
