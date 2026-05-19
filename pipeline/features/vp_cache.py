"""VP Cache — pre-computed daily + weekly volume profiles.

Stored in data/vp_cache.json. Loaded on server start.
Eliminates cold-start noise where VP shows null for the first hour.

Cache structure:
  {symbol: {daily: {"2026-05-14": {...}, ...}, weekly: {"2026-W20": {...}, ...}}}

On server start:
  1. Load existing cache
  2. Fill any missing periods from stored bars (last 5 days, last 2 weeks)
  3. Save
  4. Serve from cache on every /decide call
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_FILE = ROOT / "data" / "vp_cache.json"


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _week_key(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.year}-W{dt.strftime('%V')}"


IST_OFFSET_H = 5.5   # UTC+5:30


def _utc_session_date(ist_date_str: str, session_start_utc: int) -> str:
    """Convert IST calendar date → UTC date for the session start.

    If session_start_utc + IST_OFFSET >= 24, the session crosses midnight IST:
    the UTC date is one day earlier than the IST date.
    e.g. XAUTUSDT session_start=22: IST "May 19" = UTC "May 18" 22:00
    """
    if session_start_utc + IST_OFFSET_H >= 24:
        ist_dt = datetime.strptime(ist_date_str, "%Y-%m-%d") - timedelta(days=1)
        return ist_dt.strftime("%Y-%m-%d")
    return ist_date_str


def _day_bounds(ist_date_str: str, session_start_utc: int = 0) -> tuple[int, int]:
    """Return (start_ts, end_ts) for the session covering IST date `ist_date_str`.

    BTCUSDT (start=0):  2026-05-19 00:00 UTC → 2026-05-20 00:00 UTC
    XAUTUSDT (start=22): 2026-05-18 22:00 UTC → 2026-05-19 22:00 UTC
                         (= IST 2026-05-19 03:30 → 2026-05-20 03:30)
    """
    utc_date = _utc_session_date(ist_date_str, session_start_utc)
    dt = datetime.strptime(utc_date, "%Y-%m-%d").replace(
        hour=session_start_utc, minute=0, second=0, tzinfo=timezone.utc
    )
    start = int(dt.timestamp())
    return start, start + 86400


def _week_bounds(week_str: str, session_start_utc: int = 0) -> tuple[int, int]:
    year, week = week_str.split("-W")
    dt = datetime.strptime(f"{year}-W{week}-1", "%Y-W%W-%w").replace(
        hour=session_start_utc, minute=0, second=0, tzinfo=timezone.utc
    )
    start = int(dt.timestamp())
    return start, start + 604800


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _vp_to_dict(vp) -> dict | None:
    if vp is None:
        return None
    return {
        "poc": vp.poc, "vah": vp.vah, "val": vp.val,
        "shape": vp.shape, "current_position": vp.current_position,
        "hvn_zones": vp.hvn_zones, "lvn_zones": vp.lvn_zones,
        "naked_poc": vp.naked_poc, "total_volume": vp.total_volume,
        "price_range": vp.price_range, "bar_count": vp.bar_count,
    }


def _compute_period_vp(all_bars, start_ts: int, end_ts: int) -> dict | None:
    from .volume_profile import compute
    period_bars = [b for b in all_bars if start_ts <= b.close_ts < end_ts]
    if len(period_bars) < 30:   # need at least 30 bars for cached VP
        return None
    latest_close = period_bars[-1].ohlc.c
    vp = compute(period_bars, "cached", latest_close)
    return _vp_to_dict(vp)


def build_and_save(
    symbols: list[str],
    primary_tf: str = "1m",
    session_start_utc: dict[str, int] | None = None,
) -> None:
    """Pre-compute last 5 days + last 2 weeks VP for each symbol. Save to cache.

    session_start_utc: {symbol: utc_hour} — e.g. {"XAUTUSDT": 22, "BTCUSDT": 0}
    """
    from pipeline.state_store import store as _store
    session_cfg = session_start_utc or {}
    cache = _load()
    now_ts = int(time.time())
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    for symbol in symbols:
        s = _store()
        all_bars = s.recent(symbol, primary_tf, 100_000)
        if not all_bars:
            LOG.info(f"[vp_cache] no bars for {symbol} {primary_tf} — skip")
            continue

        from typing import Any as _Any
        sess_start: int = int(session_cfg.get(symbol, 0))
        cache.setdefault(symbol, {"daily": {}, "weekly": {}, "session_start_utc": sess_start})  # type: ignore[union-attr]
        sym_cache: dict[str, _Any] = cache[symbol]  # type: ignore[assignment]
        sym_cache["session_start_utc"] = sess_start   # update if changed
        computed = 0

        # Last 5 days (including today)
        for days_back in range(5):
            dt = now_dt - timedelta(days=days_back)
            date_key = dt.strftime("%Y-%m-%d")
            if date_key in sym_cache["daily"] and days_back > 0:
                continue   # past days already complete — skip
            start_ts, end_ts = _day_bounds(date_key, sess_start)
            vp = _compute_period_vp(all_bars, start_ts, min(end_ts, now_ts))
            if vp:
                sym_cache["daily"][date_key] = vp
                computed += 1

        # Last 2 weeks (including current week)
        for weeks_back in range(2):
            dt = now_dt - timedelta(weeks=weeks_back)
            week_key = f"{dt.year}-W{dt.strftime('%V')}"
            if week_key in sym_cache["weekly"] and weeks_back > 0:
                continue
            start_ts, end_ts = _week_bounds(week_key, sess_start)
            vp = _compute_period_vp(all_bars, start_ts, min(end_ts, now_ts))
            if vp:
                sym_cache["weekly"][week_key] = vp
                computed += 1

        d_count = len(sym_cache["daily"])  # type: ignore[arg-type]
        w_count = len(sym_cache["weekly"])  # type: ignore[arg-type]
        LOG.info(f"[vp_cache] {symbol}: computed {computed} new periods (daily: {d_count}, weekly: {w_count})")

    _save(cache)
    LOG.info(f"[vp_cache] saved → {CACHE_FILE}")


def get(symbol: str, period: str) -> dict | None:
    """Get cached VP for symbol + period (today / this week)."""
    cache = _load()
    sym = cache.get(symbol, {})
    now_ts = int(time.time())
    if period == "daily":
        key = _day_key(now_ts)
        return sym.get("daily", {}).get(key)
    elif period == "weekly":
        key = _week_key(now_ts)
        return sym.get("weekly", {}).get(key)
    return None


def get_history(symbol: str, period: str, n: int = 5) -> list[dict]:
    """Get last N cached VP snapshots (newest last)."""
    cache = _load()
    entries = cache.get(symbol, {}).get(period, {})
    sorted_keys = sorted(entries.keys())[-n:]
    return [{"period_key": k, **entries[k]} for k in sorted_keys]


def poc_sequence(symbol: str, period: str, n: int = 5) -> list[float | None]:
    return [e.get("poc") for e in get_history(symbol, period, n)]
