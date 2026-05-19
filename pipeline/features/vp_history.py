"""VP history store — snapshots daily + weekly VP at period close.

Appends to data/vp_history.jsonl when a period boundary is crossed.
Survives resets of footprint bar data since it's a separate log.

Usage:
  snapshot_if_boundary(prev_bar_ts, new_bar_ts, symbol, primary_tf)
  get_history(symbol, period="daily", n=7) -> list of VP dicts
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .volume_profile import from_store, VolumeProfilePeriod

ROOT = Path(__file__).resolve().parent.parent.parent
VP_HISTORY = ROOT / "data" / "vp_history.jsonl"


def _day_num(ts: int) -> int:
    return ts // 86400


def _week_num(ts: int) -> int:
    return ts // 604800


def _vp_to_dict(vp: VolumeProfilePeriod, symbol: str, period: str, period_end_ts: int) -> dict:
    return {
        "symbol": symbol,
        "period": period,
        "period_end_ts": period_end_ts,
        "poc": vp.poc,
        "vah": vp.vah,
        "val": vp.val,
        "shape": vp.shape,
        "hvn_zones": vp.hvn_zones,
        "lvn_zones": vp.lvn_zones,
        "naked_poc": vp.naked_poc,
        "total_volume": vp.total_volume,
        "price_range": vp.price_range,
        "bar_count": vp.bar_count,
        "logged_ts": int(time.time()),
    }


def snapshot_if_boundary(
    prev_ts: int,
    new_ts: int,
    symbol: str,
    primary_tf: str,
) -> list[str]:
    """Snapshot VP if bar crosses a day or week boundary. Returns list of periods snapshotted."""
    snapshotted = []
    VP_HISTORY.parent.mkdir(parents=True, exist_ok=True)

    # Day boundary
    if _day_num(prev_ts) != _day_num(new_ts):
        vp = from_store(symbol, primary_tf, "daily")
        if vp and vp.bar_count > 0:
            row = _vp_to_dict(vp, symbol, "daily", prev_ts)
            with VP_HISTORY.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            snapshotted.append("daily")

    # Week boundary
    if _week_num(prev_ts) != _week_num(new_ts):
        vp = from_store(symbol, primary_tf, "weekly")
        if vp and vp.bar_count > 0:
            row = _vp_to_dict(vp, symbol, "weekly", prev_ts)
            with VP_HISTORY.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            snapshotted.append("weekly")

    return snapshotted


def get_history(symbol: str, period: str = "daily", n: int = 7) -> list[dict]:
    """Return last N VP snapshots for symbol + period, newest last."""
    if not VP_HISTORY.exists():
        return []
    rows = []
    with VP_HISTORY.open() as fh:
        for line in fh:
            try:
                row = json.loads(line)
                if row.get("symbol") == symbol and row.get("period") == period:
                    rows.append(row)
            except Exception:
                pass
    return rows[-n:]


def poc_sequence(symbol: str, period: str = "daily", n: int = 7) -> list[float | None]:
    """Return last N POC values — shows where value has been migrating."""
    return [r.get("poc") for r in get_history(symbol, period, n)]
