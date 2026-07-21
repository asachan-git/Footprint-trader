"""As-of VP seam for the grid backtest — makes vp_cache reads point-in-time.

`with vp_asof(replay_cache_path):` swaps vp_cache.get / get_prev_and_today /
get_history for versions that:
  1. read the prebuilt forming-snapshot cache (backtest/vp_replay.py), and
  2. select, for the requested session-day, the newest forming snapshot whose
     as-of bucket is <= the SIMULATED clock (execution.clock.now()).

All ~18 lazy `from pipeline.features import vp_cache` call sites in zone_triggers
and grid_planner resolve module attributes at call time, so rebinding these three
functions intercepts every one with no call-site change. The profile maths is never
reimplemented — snapshots came from the real _compute_period_vp, and offset shifting
reuses vp_cache._shift_vp.

Also asserts, on enter, that writes are isolated to a scratch dir (FB_DATA_DIR), so a
backtest can never append to live cache/audit state.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from pipeline.features import vp_cache as vpc
from execution import clock


def _pick_asof(snaps: dict, now_ts: int) -> dict | None:
    """Newest forming snapshot with as-of bucket <= now_ts. None if none yet."""
    best_k = -1
    for k in snaps:
        ik = int(k)
        if ik <= now_ts and ik > best_k:
            best_k = ik
    return snaps[str(best_k)] if best_k >= 0 else None


@contextmanager
def vp_asof(replay_cache_path: str | Path):
    """Install point-in-time vp_cache reads from a forming-snapshot replay cache."""
    if not os.environ.get("FB_DATA_DIR"):
        raise RuntimeError("vp_asof requires FB_DATA_DIR (scratch) — refusing to run against live data/")

    cache = json.loads(Path(replay_cache_path).read_text())

    def _sym(symbol: str) -> dict:
        return cache.get(symbol, {})

    def _offset(sym: dict) -> float:
        return float(sym.get("venue_price_offset") or 0.0)

    def _anchor(sym: dict):
        return vpc._normalize_anchor(sym.get("session_start_utc") or 0)

    def get(symbol: str, period: str):
        sym = _sym(symbol)
        now_ts = int(clock.now())
        anchor = _anchor(sym)
        off = _offset(sym)
        if period == "daily":
            key = vpc._session_day_key(now_ts, anchor)
            snaps = sym.get("daily", {}).get(key)
            vp = _pick_asof(snaps, now_ts) if snaps else None
        elif period == "weekly":
            key = vpc._week_key(now_ts)
            vp = sym.get("weekly", {}).get(key)   # weekly stored whole (see vp_replay)
        else:
            return None
        return vpc._shift_vp(vp, off) if vp else None

    def get_prev_and_today(symbol: str):
        sym = _sym(symbol)
        now_ts = int(clock.now())
        anchor = _anchor(sym)
        off = _offset(sym)
        daily = sym.get("daily", {})

        today_key = vpc._session_day_key(now_ts, anchor)
        prev_key = vpc._prev_trading_day_key(now_ts, anchor)

        today_snaps = daily.get(today_key)
        today_vp = _pick_asof(today_snaps, now_ts) if today_snaps else None
        # prev-D is complete relative to `now` → its last (full-day) snapshot.
        prev_snaps = daily.get(prev_key)
        prev_vp = _pick_asof(prev_snaps, now_ts) if prev_snaps else None

        return (vpc._shift_vp(prev_vp, off) if prev_vp else None,
                vpc._shift_vp(today_vp, off) if today_vp else None)

    def get_history(symbol: str, period: str, n: int = 5):
        sym = _sym(symbol)
        now_ts = int(clock.now())
        off = _offset(sym)
        entries = sym.get(period, {})
        # each daily entry is a snapshot-dict; resolve each to its as-of view.
        keys = sorted(entries.keys())
        out = []
        for k in keys[-n:]:
            e = entries[k]
            vp = _pick_asof(e, now_ts) if isinstance(e, dict) and any(
                s.isdigit() for s in e.keys()) else e
            if vp:
                out.append({"period_key": k, **vpc._shift_vp(vp, off)})
        return out

    _orig = (vpc.get, vpc.get_prev_and_today, vpc.get_history)
    vpc.get, vpc.get_prev_and_today, vpc.get_history = get, get_prev_and_today, get_history
    try:
        yield
    finally:
        vpc.get, vpc.get_prev_and_today, vpc.get_history = _orig


@contextmanager
def store_asof():
    """Truncate the bar store to the SIMULATED clock — no lookahead.

    zone_triggers reads bars via store().latest() / .recent(); both must return only
    bars closed at or before clock.now(), or a replay sees the future. Wraps the live
    StateStore methods to clip by close_ts <= now. (dryrun_fleet uses the same
    monkeypatch idea for its causal cutoff.)
    """
    if not os.environ.get("FB_DATA_DIR"):
        raise RuntimeError("store_asof requires FB_DATA_DIR (scratch)")
    from pipeline.state_store import store as _store_fn, StateStore

    st = _store_fn()
    _orig_latest = st.latest
    _orig_recent = st.recent
    _orig_asof = st.as_of

    def _now_ts() -> int:
        return int(clock.now())

    def latest(symbol: str, tf: str):
        return _orig_asof(symbol, tf, _now_ts())     # most-recent bar <= now

    def recent(symbol: str, tf: str, n: int):
        cut = _now_ts()
        series = st._bars.get((symbol, tf), [])
        # bars are ascending; take those closed <= now, last n
        import bisect as _b
        hi = _b.bisect_right([b.close_ts for b in series], cut)
        return list(series[:hi][-n:])

    def as_of(symbol: str, tf: str, ts: int):
        return _orig_asof(symbol, tf, min(ts, _now_ts()))

    st.latest, st.recent, st.as_of = latest, recent, as_of
    try:
        yield
    finally:
        st.latest, st.recent, st.as_of = _orig_latest, _orig_recent, _orig_asof
