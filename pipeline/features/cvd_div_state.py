"""Last-CVD-divergence memory — remembers the most recent CVD divergence per
symbol so consumers aren't brittle to exact-bar timing.

scan_divergences() is stateless (it re-derives markers from a bar window each
call). This module caches the latest bull and bear divergence per symbol as they
are scanned, so a filter can accept a divergence that printed a bar or two ago
(not only on the current forming candle), and so the "last div value" is always
queryable (strategies, exits, dashboard).

Updated via record_from_scan() on each scan; read via last() / aligned_within().
Module-global state, mirroring the sweep registry pattern.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

# symbol -> {"bull": <div dict>, "bear": <div dict>}  (div = scan_divergences marker)
_last: dict[str, dict[str, dict]] = {}
_lock = threading.Lock()

# Persisted so the "last div value" survives a server restart (the 2-bar filter
# repopulates on its own, but last()/the dashboard keep the prior value meanwhile).
_PATH = Path(__file__).resolve().parents[2] / "data" / "cvd_div_state.json"


def _load() -> None:
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text())
            if isinstance(data, dict):
                _last.update(data)
    except Exception:
        pass


def _save_locked() -> None:
    """Atomic write (tmp + rename). Caller holds _lock."""
    try:
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_last, separators=(",", ":")))
        tmp.replace(_PATH)
    except Exception:
        pass


_load()


def record_from_scan(symbol: str, divs: list[dict]) -> None:
    """Update the remembered last bull/bear divergence from a scan_divergences result.
    Keeps the most recent marker of each type (by ts). Persists on change."""
    if not divs:
        return
    with _lock:
        cur = _last.setdefault(symbol, {})
        changed = False
        for m in divs:
            t = m.get("type")
            if t not in ("bull", "bear"):
                continue
            prev = cur.get(t)
            if prev is None or m["ts"] >= prev["ts"]:
                cur[t] = dict(m)
                changed = True
        if changed:
            _save_locked()


def last(symbol: str, div_type: str | None = None) -> dict | None:
    """The remembered last divergence for `symbol`. With div_type ('bull'|'bear')
    returns that type; otherwise the most recent of either type. None if none seen."""
    with _lock:
        cur = _last.get(symbol, {})
        if div_type:
            return cur.get(div_type)
        vals = list(cur.values())
        return max(vals, key=lambda m: m["ts"]) if vals else None


def aligned_within(symbol: str, side: str, ref_ts: int, max_age_secs: int,
                   strength_gate: float = 0.0) -> dict | None:
    """Remembered divergence that OPPOSES `side` (bear vs short, bull vs long),
    fresh within `max_age_secs` of ref_ts and ≥ strength_gate. Else None."""
    want = "bear" if side == "short" else "bull"
    rec = last(symbol, want)
    if rec is None or rec.get("strength", 0.0) < strength_gate:
        return None
    if ref_ts - rec["ts"] > max_age_secs:
        return None
    return rec
