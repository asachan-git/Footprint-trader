"""A/B report: Mode 1 (Claude) vs Mode 2 (rules) — instrument-level analysis.

Reads:
  data/decisions.jsonl     (Mode 1 — Claude via /decide_multi)
  data/mode_compare.jsonl  (Mode 2 — rules via /grid_tick dry-run)
  data/positions.jsonl     (Mode 1 actual fills + closes)

Usage:
  python scripts/compare_modes.py [--symbol BTCUSDT|XAUTUSDT] [--last N] [--full]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M")


# ── loaders ────────────────────────────────────────────────────────────────────

def load_mode1(symbol_filter=None) -> dict:
    """(symbol, bar_id) → {side, conf, validator, ts}"""
    out = {}
    path = ROOT / "data" / "decisions.jsonl"
    if not path.exists():
        return out
    for line in path.open():
        try:
            r = json.loads(line)
            sym, bar = r.get("symbol", ""), r.get("bar_id", "")
            if not sym or not bar:
                continue
            if symbol_filter and sym != symbol_filter:
                continue
            d = r.get("decision") or {}
            out[(sym, bar)] = {
                "side": d.get("side"),
                "conf": d.get("confidence", 0) or 0,
                "validator": r.get("validator_reason"),
                "ts": r.get("ts", 0),
            }
        except Exception:
            pass
    return out


def load_mode2(symbol_filter=None) -> dict:
    """(symbol, bar_id) → {side, score, bias, votes, ts}"""
    out = {}
    path = ROOT / "data" / "mode_compare.jsonl"
    if not path.exists():
        return out
    for line in path.open():
        try:
            r = json.loads(line)
            sym, bar = r.get("symbol", ""), r.get("bar_id", "")
            if not sym or not bar:
                continue
            if symbol_filter and sym != symbol_filter:
                continue
            key = (sym, bar)
            # keep highest |score| if duplicate bars
            existing = out.get(key)
            if existing and abs(existing["score"]) >= abs(r.get("score", 0)):
                continue
            out[key] = {
                "side": r.get("side"),
                "score": r.get("score", 0) or 0,
                "bias": r.get("bias_strength", 0),
                "votes": r.get("votes", []),
                "ts": r.get("ts", 0),
            }
        except Exception:
            pass
    return out


def load_positions() -> tuple[dict, dict]:
    """Returns (opens, closes) dicts keyed by position_id."""
    opens, closes = {}, {}
    path = ROOT / "data" / "positions.jsonl"
    if not path.exists():
        return opens, closes
    for line in path.open():
        try:
            r = json.loads(line)
            pid = r.get("position_id", "")
            if not pid:
                continue
            if r.get("type") == "open":
                opens[pid] = r
            elif r.get("type") == "close":
                closes[pid] = r
        except Exception:
            pass
    return opens, closes


# ── signal stats ───────────────────────────────────────────────────────────────

def signal_stats(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        return {"n": 0}
    sides = [r["side"] for r in records]
    non_flat = [s for s in sides if s != "flat"]
    return {
        "n": n,
        "long": sides.count("long"),
        "short": sides.count("short"),
        "flat": sides.count("flat"),
        "flat_pct": sides.count("flat") / n * 100,
        "active_pct": len(non_flat) / n * 100,
    }


def m1_pnl_stats(opens: dict, closes: dict, symbol: str) -> dict:
    sym_opens = {pid: o for pid, o in opens.items() if o.get("symbol") == symbol}
    matched = [(sym_opens[pid], closes[pid]) for pid in closes if pid in sym_opens]
    if not matched:
        return {}
    rrs = [c.get("realized_r", 0) or 0 for _, c in matched]
    wins = sum(1 for r in rrs if r > 0)
    return {
        "closed": len(matched),
        "open": len(sym_opens) - len(matched),
        "win": wins,
        "loss": len(matched) - wins,
        "win_pct": wins / len(matched) * 100,
        "avg_rr": sum(rrs) / len(rrs),
        "total_rr": sum(rrs),
        "max_win": max(rrs),
        "max_loss": min(rrs),
    }


# ── agreement analysis ─────────────────────────────────────────────────────────

def agreement_stats(m1: dict, m2: dict, symbol: str | None = None) -> dict:
    keys = set(m1) & set(m2)
    if symbol:
        keys = {k for k in keys if k[0] == symbol}
    agree = sum(1 for k in keys if m1[k]["side"] == m2[k]["side"])
    disagree = len(keys) - agree
    # Cases where they disagree with non-flat
    conflict = sum(
        1 for k in keys
        if m1[k]["side"] != m2[k]["side"]
        and m1[k]["side"] not in (None, "flat")
        and m2[k]["side"] not in (None, "flat")
    )
    return {
        "aligned": len(keys),
        "agree": agree,
        "disagree": disagree,
        "agree_pct": agree / len(keys) * 100 if keys else 0,
        "active_conflict": conflict,
    }


# ── vote module breakdown ──────────────────────────────────────────────────────

def vote_breakdown(m2_records: list[dict]) -> dict:
    """Which modules vote most in Mode 2, and in which direction."""
    module_dir = defaultdict(list)
    for r in m2_records:
        for v in r.get("votes", []):
            module_dir[v["m"]].append(v["d"])
    out = {}
    for mod, dirs in sorted(module_dir.items()):
        n = len(dirs)
        bull = sum(1 for d in dirs if d > 0)
        bear = sum(1 for d in dirs if d < 0)
        out[mod] = {"n": n, "bull": bull, "bear": bear, "bias": sum(dirs) / n}
    return out


# ── main ───────────────────────────────────────────────────────────────────────

def _section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="filter to one symbol")
    ap.add_argument("--last", type=int, default=0, help="last N aligned bars for signal table")
    ap.add_argument("--full", action="store_true", help="print per-bar signal table")
    args = ap.parse_args()

    m1 = load_mode1(args.symbol)
    m2 = load_mode2(args.symbol)
    opens, closes = load_positions()

    symbols = sorted({k[0] for k in set(m1) | set(m2)}) if not args.symbol else [args.symbol]

    _section("INSTRUMENT-LEVEL SIGNAL STATS")
    for sym in symbols:
        m1_recs = [v for (s, _), v in m1.items() if s == sym]
        m2_recs = [v for (s, _), v in m2.items() if s == sym]
        s1 = signal_stats(m1_recs)
        s2 = signal_stats(m2_recs)
        ag = agreement_stats(m1, m2, sym)
        print(f"\n  {sym}")
        print(f"    M1 (Claude):  {s1.get('n',0)} signals | long={s1.get('long',0)} short={s1.get('short',0)} flat={s1.get('flat',0)} | active={s1.get('active_pct',0):.0f}%")
        print(f"    M2 (rules):   {s2.get('n',0)} signals | long={s2.get('long',0)} short={s2.get('short',0)} flat={s2.get('flat',0)} | active={s2.get('active_pct',0):.0f}%")
        print(f"    Agreement:    {ag['agree']}/{ag['aligned']} ({ag['agree_pct']:.0f}%) | active conflicts: {ag['active_conflict']}")

    _section("MODE 1 ACTUAL OUTCOMES (positions.jsonl)")
    for sym in symbols:
        p = m1_pnl_stats(opens, closes, sym)
        if not p:
            print(f"\n  {sym}: no closed positions")
            continue
        print(f"\n  {sym}")
        print(f"    Closed: {p['closed']} | Open: {p['open']}")
        print(f"    Win/Loss: {p['win']}/{p['loss']} ({p['win_pct']:.0f}% WR)")
        print(f"    Avg RR: {p['avg_rr']:+.2f} | Total RR: {p['total_rr']:+.2f}")
        print(f"    Best: {p['max_win']:+.2f}R | Worst: {p['max_loss']:+.2f}R")

    _section("MODE 2 VOTE MODULE BREAKDOWN (per instrument)")
    for sym in symbols:
        m2_recs = [v for (s, _), v in m2.items() if s == sym]
        if not m2_recs:
            continue
        print(f"\n  {sym}")
        for mod, stats in vote_breakdown(m2_recs).items():
            bar = "▲" * stats["bull"] + "▼" * stats["bear"]
            print(f"    {mod:<22} n={stats['n']:>3} bull={stats['bull']:>3} bear={stats['bear']:>3} bias={stats['bias']:+.2f}  {bar[:20]}")

    _section("NOTE: MODE 2 OUTCOME DATA")
    print("  Mode 2 runs dry_run=True → no paper fills → no PnL history.")
    print("  To enable proper A/B: set dry_run=False in start.sh grid_tick loop,")
    print("  or run a separate paper instance tagged by mode.")

    if args.full or args.last:
        _section("PER-BAR SIGNAL TABLE")
        all_keys = sorted(set(m1) | set(m2), key=lambda k: (m1.get(k, {}).get("ts") or m2.get(k, {}).get("ts") or 0))
        if args.symbol:
            all_keys = [k for k in all_keys if k[0] == args.symbol]
        if args.last:
            all_keys = all_keys[-args.last:]
        print(f"\n  {'IST':<17} {'Symbol':<10} {'M1':<22} {'M2':<24} {'Agree'}")
        print(f"  {'-'*88}")
        for k in all_keys:
            sym, _ = k
            r1, r2 = m1.get(k), m2.get(k)
            ts = (r1 or r2 or {}).get("ts", 0)
            t = _ts_ist(ts) if ts else "-"
            if r1 and r2:
                s1 = r1["side"] + (f"({r1['conf']:.2f})" if r1["side"] != "flat" else "")
                s2 = r2["side"] + (f"({r2['score']:+.2f}/{r2['bias']})" if r2["side"] != "flat" else "")
                mark = "✓" if r1["side"] == r2["side"] else "✗"
                print(f"  {t:<17} {sym:<10} {s1:<22} {s2:<24} {mark}")
            elif r1:
                s1 = r1["side"] + (f"({r1['conf']:.2f})" if r1["side"] != "flat" else "")
                print(f"  {t:<17} {sym:<10} {s1:<22} {'(no M2)':<24}")
            elif r2:
                s2 = r2["side"] + (f"({r2['score']:+.2f}/{r2['bias']})" if r2["side"] != "flat" else "")
                print(f"  {t:<17} {sym:<10} {'(no M1)':<22} {s2:<24}")


if __name__ == "__main__":
    main()
