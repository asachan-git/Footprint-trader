"""Per-strategy results — stats + equity curve from a strategy's own cycles.jsonl.

Same metric set as the global SYSTEM_REPORT (WR, avg R, payoff, profit factor,
buy/sell split, exit reasons, equity curve) but scoped to one strategy's files,
so strategies are compared apples-to-apples on isolated data.
"""

from __future__ import annotations

import json
from collections import Counter

from .base import StrategyContext


def _stat(rows: list[dict]) -> dict | None:
    n = len(rows)
    if not n:
        return None
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    tot = sum(r["pnl"] for r in rows)
    gp = sum(r["pnl"] for r in wins)
    gl = abs(sum(r["pnl"] for r in losses))
    aw = gp / len(wins) if wins else 0.0
    al = -gl / len(losses) if losses else 0.0
    return {
        "trades": n,
        "wr": round(len(wins) / n * 100, 1),
        "total_r": round(tot, 4),
        "avg_r": round(tot / n, 4),
        "wins": len(wins), "losses": len(losses),
        "avg_win": round(aw, 4), "avg_loss": round(al, 4),
        "payoff": round(aw / -al, 2) if al < 0 else None,
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
    }


def _load_cycles(ctx: StrategyContext) -> list[dict]:
    path = ctx.cstore._log_path
    if not path.exists():
        return []
    opens, closes = {}, []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "open":
            opens[d["cycle_id"]] = d
        elif d.get("type") in ("close", "circuit"):
            closes.append(d)
    rows = []
    for c in closes:
        o = opens.get(c["cycle_id"], {})
        rows.append({
            "sym": o.get("symbol", "?"),
            "dir": o.get("direction", "?"),
            "pnl": c.get("realized_pnl", 0.0),
            "reason": c.get("reason", "?"),
            "ts": c.get("ts", 0),
        })
    rows.sort(key=lambda r: r["ts"])
    return rows


def _ascii_equity(curve: list[float], width: int = 48, height: int = 12) -> str:
    if not curve:
        return "(no closed cycles yet)"
    pts = [0.0] + curve
    mn, mx = min(pts), max(pts)
    rng = (mx - mn) or 1.0
    n = len(pts)
    cols = [pts[round(i * (n - 1) / (width - 1))] for i in range(width)]
    grid = [[" "] * width for _ in range(height)]
    for x, v in enumerate(cols):
        y = height - 1 - round((v - mn) / rng * (height - 1))
        grid[y][x] = "+"
    lines = []
    for r in range(height):
        val = mx - (r / (height - 1)) * rng
        lines.append(f"{val:+6.2f} |" + "".join(grid[r]))
    lines.append("       +" + "-" * width)
    return "\n".join(lines)


def compute(ctx: StrategyContext) -> dict:
    rows = _load_cycles(ctx)
    syms = sorted(set(r["sym"] for r in rows))
    cum, curve = 0.0, []
    for r in rows:
        cum += r["pnl"]
        curve.append(cum)
    peak, maxdd = -1e9, 0.0
    for v in curve:
        peak = max(peak, v)
        maxdd = min(maxdd, v - peak)
    return {
        "strategy": ctx.name,
        "overall": _stat(rows),
        "by_symbol": {s: _stat([r for r in rows if r["sym"] == s]) for s in syms},
        "by_direction": {
            "long": _stat([r for r in rows if r["dir"] == "long"]),
            "short": _stat([r for r in rows if r["dir"] == "short"]),
        },
        "exit_reasons": dict(Counter(r["reason"] for r in rows)),
        "equity": {
            "final_r": round(cum, 4),
            "max_dd_r": round(maxdd, 4),
            "points": len(curve),
        },
        "equity_ascii": _ascii_equity(curve),
    }
