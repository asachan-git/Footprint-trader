"""A/B report: Mode 1 (Claude) vs Mode 2 (rules) decisions per bar.

Reads:
  data/decisions.jsonl     (Mode 1 — Claude via /decide_multi)
  data/mode_compare.jsonl  (Mode 2 — rules via /grid_tick dry-run)

Aligns by (symbol, bar_id) and prints agreement / disagreement.

Usage: python scripts/compare_modes.py [--symbol BTCUSDT] [--last N]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def _ts_to_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M")


def load_mode1(path: Path, symbol_filter: str | None = None) -> dict:
    """Map (symbol, bar_id) → Mode 1 record."""
    out = {}
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
                sym = r.get("symbol", "")
                bar = r.get("bar_id", "")
                if symbol_filter and sym != symbol_filter:
                    continue
                # Only consider validator-passing rows
                d = r.get("decision", {})
                out[(sym, bar)] = {
                    "side": d.get("side"),
                    "conf": d.get("confidence", 0),
                    "validator": r.get("validator_reason"),
                    "ts": r.get("ts", 0),
                }
            except Exception:
                pass
    return out


def load_mode2(path: Path, symbol_filter: str | None = None) -> dict:
    out = {}
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
                sym = r.get("symbol", "")
                bar = r.get("bar_id", "")
                if symbol_filter and sym != symbol_filter:
                    continue
                out[(sym, bar)] = {
                    "side": r.get("side"),
                    "score": r.get("score", 0),
                    "bias": r.get("bias_strength", 0),
                    "votes": r.get("votes", []),
                    "ts": r.get("ts", 0),
                }
            except Exception:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--last", type=int, default=20)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent / "data"
    m1 = load_mode1(root / "decisions.jsonl", args.symbol)
    m2 = load_mode2(root / "mode_compare.jsonl", args.symbol)

    # Align keys (symbol, bar_id)
    keys = sorted(set(m1) | set(m2), key=lambda k: (m1.get(k, {}).get("ts") or m2.get(k, {}).get("ts") or 0))
    if args.last:
        keys = keys[-args.last:]

    print(f"{'IST time':<17} {'Symbol':<10} {'M1 (Claude)':<22} {'M2 (rules)':<22} {'Agree'}")
    print("-" * 92)
    agree = disagree = m1_only = m2_only = 0
    for k in keys:
        sym, bar = k
        r1 = m1.get(k)
        r2 = m2.get(k)
        ts = (r1 or r2).get("ts", 0)
        time_str = _ts_to_ist(ts) if ts else "-"

        if r1 and r2:
            s1 = (r1["side"] or "flat") + (f"({r1['conf']:.2f})" if r1["side"] != "flat" else "")
            s2 = (r2["side"] or "flat") + (f"({r2['score']:+.2f}/{r2['bias']})" if r2["side"] != "flat" else "")
            same = r1["side"] == r2["side"]
            mark = "✓" if same else "✗"
            if same: agree += 1
            else: disagree += 1
            print(f"{time_str:<17} {sym:<10} {s1:<22} {s2:<22} {mark}")
        elif r1:
            m1_only += 1
            s1 = (r1["side"] or "flat") + (f"({r1['conf']:.2f})" if r1["side"] != "flat" else "")
            print(f"{time_str:<17} {sym:<10} {s1:<22} {'(no M2)':<22}")
        elif r2:
            m2_only += 1
            s2 = (r2["side"] or "flat") + (f"({r2['score']:+.2f}/{r2['bias']})" if r2["side"] != "flat" else "")
            print(f"{time_str:<17} {sym:<10} {'(no M1)':<22} {s2:<22}")

    total = agree + disagree
    print()
    print(f"Aligned: {total} bars | Agree: {agree} ({agree/total*100 if total else 0:.0f}%) | "
          f"Disagree: {disagree} | M1 only: {m1_only} | M2 only: {m2_only}")


if __name__ == "__main__":
    main()
