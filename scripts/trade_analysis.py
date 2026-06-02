"""Trade analysis across strategies / sources.

Reads the trade logs that actually carry per-trade outcomes and prints
grouped win-rate / expectancy stats:

  - data/positions.jsonl          live grid trades, grouped by `source`
                                  (m1_claude, mechanical_grid, ...). Source +
                                  symbol live on the OPEN row; outcome
                                  (realized_r, reason) on the CLOSE row — paired
                                  here by position_id.
  - data/strategies/<name>/backtest_trades.jsonl   per-strategy backtests
    (coup today). Already in closed-trade shape (r, side, reason).

Usage: python scripts/trade_analysis.py
"""
from __future__ import annotations

import json
import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "n": n,
        "wr%": round(100 * len(wins) / n),
        "sumR": round(sum(rs), 2),
        "avgR": round(sum(rs) / n, 3),
        "avgW": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avgL": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
    }


def _fmt(label: str, s: dict) -> str:
    if not s:
        return f"  {label:24s} (no trades)"
    return (f"  {label:24s} n={s['n']:>3} wr={s['wr%']:>3}% "
            f"sumR={s['sumR']:>7} avgR={s['avgR']:>7} "
            f"avgW={s['avgW']:>5} avgL={s['avgL']:>6} pf={s['pf']}")


def _reason_bucket(reason: str | None) -> str:
    r = (reason or "").lower()
    if "tp" in r:
        return "tp"
    if "sl" in r:
        return "sl"
    if "flip" in r:
        return "flip"
    if "absorption" in r:
        return "absorption"
    return r.split()[0] if r else "other"


def analyze_live() -> None:
    f = DATA / "positions.jsonl"
    if not f.exists():
        print("(no data/positions.jsonl)")
        return
    meta: dict[str, dict] = {}     # position_id -> {source, symbol, side}
    closed: list[dict] = []
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        pid = d.get("position_id")
        if d.get("type") == "open":
            meta[pid] = {"source": d.get("source") or "untagged",
                         "symbol": d.get("symbol"), "side": d.get("side")}
        elif d.get("type") == "close" and d.get("realized_r") is not None:
            m = meta.get(pid, {})
            closed.append({"source": m.get("source", "untagged"),
                           "symbol": m.get("symbol"), "side": m.get("side"),
                           "r": d["realized_r"], "reason": d.get("reason")})

    print(f"\n=== LIVE  data/positions.jsonl — {len(closed)} closed trades ===")
    by_src = collections.defaultdict(list)
    by_src_sym = collections.defaultdict(list)
    by_src_reason = collections.defaultdict(collections.Counter)
    for t in closed:
        by_src[t["source"]].append(t["r"])
        by_src_sym[(t["source"], t["symbol"])].append(t["r"])
        by_src_reason[t["source"]][_reason_bucket(t["reason"])] += 1

    for src in sorted(by_src):
        print(_fmt(src, _stats(by_src[src])))
        for (s, sym), rs in sorted(by_src_sym.items()):
            if s == src:
                print(_fmt(f"  · {sym}", _stats(rs)))
        print(f"    exits: {dict(by_src_reason[src])}")


def analyze_backtests() -> None:
    sdir = DATA / "strategies"
    if not sdir.exists():
        return
    for f in sorted(sdir.glob("*/backtest_trades.jsonl")):
        name = f.parent.name
        rows = []
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        rs = [r["r"] for r in rows if r.get("r") is not None]
        print(f"\n=== BACKTEST  {name}  ({f.relative_to(ROOT)}) — {len(rows)} trades ===")
        print(_fmt("all", _stats(rs)))
        # by side
        for side in ("long", "short"):
            srs = [r["r"] for r in rows if r.get("side") == side and r.get("r") is not None]
            if srs:
                print(_fmt(f"  · {side}", _stats(srs)))
        # by exit reason bucket
        by_reason = collections.defaultdict(list)
        for r in rows:
            if r.get("r") is not None:
                by_reason[_reason_bucket(r.get("reason"))].append(r["r"])
        for rb, rrs in sorted(by_reason.items()):
            print(_fmt(f"  exit:{rb}", _stats(rrs)))


if __name__ == "__main__":
    analyze_live()
    analyze_backtests()
