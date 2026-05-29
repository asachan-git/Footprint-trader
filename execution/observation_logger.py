"""Observation logger — snapshots system state for iteration comparison.

On startup (and optionally every N decisions), writes a JSON snapshot to
`data/observations/iter_{ts}.json` containing:
  - git_sha + settings excerpt (vote_weights, trading_mode, etc.)
  - decisions distribution last 24h (per symbol, per side, sum R)
  - cycle outcomes (tp/sl/invalid/open, median R, max legs used)
  - Mode 2 vote distribution (long/short/abstain per module)
  - open positions snapshot

scripts/compare_iterations.py diffs two snapshots to surface what changed
between code iterations.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBS_DIR = ROOT / "data" / "observations"
IST = timezone(timedelta(hours=5, minutes=30))


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _decisions_distribution(since: int) -> dict:
    rows = [d for d in _load_jsonl(ROOT / "data" / "decisions.jsonl") if d.get("ts", 0) >= since]
    out: dict = defaultdict(lambda: defaultdict(int))
    for d in rows:
        sym = d.get("symbol", "?")
        dec = d.get("decision", {})
        side = dec.get("side") or "flat"
        out[sym][side] += 1
        if d.get("validator_reason"):
            out[sym]["rejected"] += 1
    return {sym: dict(stats) for sym, stats in out.items()}


def _cycle_outcomes(since: int) -> dict:
    events = _load_jsonl(ROOT / "data" / "positions.jsonl")
    positions: dict = {}
    for e in events:
        pid = e.get("position_id", "")
        t = e.get("type", "")
        if t == "open" and e.get("ts", 0) >= since:
            positions[pid] = {
                "symbol": e.get("symbol"), "side": e.get("side"),
                "open_ts": e.get("ts"), "status": "open",
                "realized_r": None, "close_reason": None, "leg_count": 1,
            }
        elif t == "add_leg" and pid in positions:
            positions[pid]["leg_count"] += 1
        elif t in ("close", "invalidate") and pid in positions:
            positions[pid]["status"] = "closed" if t == "close" else "invalidated"
            positions[pid]["realized_r"] = e.get("realized_r")
            positions[pid]["close_reason"] = e.get("reason", "")

    tp_hit = sl_hit = invalidated = still_open = 0
    rs = []
    max_legs = 0
    for p in positions.values():
        if p["leg_count"] > max_legs:
            max_legs = p["leg_count"]
        if p["status"] == "open":
            still_open += 1
        elif p["status"] == "invalidated":
            invalidated += 1
        elif p["status"] == "closed":
            reason = str(p.get("close_reason", "") or "")
            if "tp" in reason:
                tp_hit += 1
            elif "sl" in reason:
                sl_hit += 1
        if p["realized_r"] is not None:
            rs.append(p["realized_r"])
    rs_sorted = sorted(rs)
    median_r = rs_sorted[len(rs_sorted) // 2] if rs_sorted else None
    return {
        "tp_hit": tp_hit, "sl_hit": sl_hit,
        "invalidated": invalidated, "still_open": still_open,
        "total_cycles": len(positions),
        "median_R": round(median_r, 3) if median_r is not None else None,
        "sum_R": round(sum(rs), 3) if rs else 0.0,
        "max_legs_used": max_legs,
    }


def _mode2_vote_distribution(since: int) -> dict:
    rows = [d for d in _load_jsonl(ROOT / "data" / "mode_compare.jsonl") if d.get("ts", 0) >= since]
    out: dict = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for v in r.get("votes") or []:
            module = v.get("m", "?")
            d = v.get("d", 0)
            if d > 0:
                out[module]["long"] += 1
            elif d < 0:
                out[module]["short"] += 1
            else:
                out[module]["abstain"] += 1
    # Track modules that NEVER voted across all rows
    all_modules = {"cvd", "vp_shape", "vp_position", "structure", "fvg", "wave", "sweep"}
    for m in all_modules:
        if m not in out:
            out[m] = {"long": 0, "short": 0, "abstain": 0}
    return {m: dict(stats) for m, stats in out.items()}


def _open_positions_snapshot() -> list[dict]:
    try:
        from execution.position_store import position_store
        out = []
        for p in position_store().open_positions():
            out.append({
                "id": p.position_id[:8], "symbol": p.symbol, "side": p.side,
                "avg_entry": round(p.avg_entry, 4),
                "tp": round(p.take_profit, 4),
                "sl": round(p.stop_loss, 4),
                "legs_filled": p.leg_count,
            })
        return out
    except Exception:
        return []


def _settings_excerpt() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
        return {
            "trading_mode": cfg.get("trading_mode"),
            "mode": cfg.get("mode"),
            "vp_bin_size": (cfg.get("vp_cache") or {}).get("vp_bin_size"),
            "analysis": cfg.get("analysis"),
            "regime": cfg.get("regime"),
            "cycle": cfg.get("cycle"),
            "decide_filter": cfg.get("decide_filter"),
        }
    except Exception:
        return {}


def write_snapshot(tag: str = "auto") -> Path:
    OBS_DIR.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    since_24h = now - 24 * 3600

    snapshot = {
        "iter_ts": now,
        "ist": datetime.fromtimestamp(now, tz=IST).strftime("%Y-%m-%d %H:%M IST"),
        "tag": tag,
        "git_sha": _git_sha(),
        "settings_excerpt": _settings_excerpt(),
        "decisions_last_24h": _decisions_distribution(since_24h),
        "cycles_outcome_last_24h": _cycle_outcomes(since_24h),
        "mode2_vote_distribution_last_24h": _mode2_vote_distribution(since_24h),
        "open_positions": _open_positions_snapshot(),
    }
    out = OBS_DIR / f"iter_{now}.json"
    out.write_text(json.dumps(snapshot, indent=2))

    # Prune to last 50
    files = sorted(OBS_DIR.glob("iter_*.json"))
    if len(files) > 50:
        for old in files[:-50]:
            try:
                old.unlink()
            except Exception:
                pass

    return out


if __name__ == "__main__":
    import sys
    tag = sys.argv[1] if len(sys.argv) > 1 else "manual"
    p = write_snapshot(tag=tag)
    print(f"Wrote {p}")
