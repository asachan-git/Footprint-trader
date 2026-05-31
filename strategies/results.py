"""Per-strategy results — stats + equity curve from a strategy's own positions.

Same metric set as the global SYSTEM_REPORT (WR, avg R, payoff, profit factor,
buy/sell split, exit reasons, equity curve) but scoped to one strategy's data,
so strategies are compared apples-to-apples on isolated data.

Source: the strategy's PositionStore closes (positions.jsonl) — the only place
realized R is written by the live exit path. (cycle_store.close_cycle is not
called live; cycles.jsonl is populated only by offline backfill scripts.)
"""

from __future__ import annotations

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


def _load_trades(ctx: StrategyContext) -> list[dict]:
    """Closed positions for this strategy, oldest first, as flat stat rows."""
    closed = ctx.pstore.closed_positions(n=10_000)
    rows = [{
        "sym": p.symbol,
        "dir": p.side,
        "pnl": p.realized_r,
        "reason": p.close_reason or "?",
        "ts": p.closed_ts or 0,
    } for p in closed]
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
    rows = _load_trades(ctx)
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
