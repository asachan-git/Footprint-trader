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
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
# Data root honours FB_DATA_DIR so a backtest points at scratch and cannot rewrite
# the live cache. CACHE_FILE stays a module attribute on purpose: backtest/seams.py
# rebinds it (vp_cache.CACHE_FILE = ...) for point-in-time replay caches.
import os as _os
_DATA_DIR = Path(_os.environ["FB_DATA_DIR"]) if _os.environ.get("FB_DATA_DIR") else ROOT / "data"
CACHE_FILE = _DATA_DIR / "vp_cache.json"


def _vp_min_bars() -> int:
    """Minimum bars required to build a cached VP (HVN/LVN/POC). Config knob
    `vp_min_bars` (default 5). Below this a profile is a single-cluster degenerate,
    so zones are withheld. Lower = zones appear sooner after a session roll at the
    cost of thinner structure. Read fresh (cheap; settings is small)."""
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load((ROOT / "config" / "settings.yaml").read_text()) or {}
        return int(_cfg.get("vp_min_bars", 5))
    except Exception:
        return 5


_IST = timezone(timedelta(hours=5, minutes=30))

# Symbols that legitimately trade through the weekend — everything else (gold via
# Binance's XAUUSDT perp) is exempted from Sat/Sun VP builds since the real market
# is closed and that volume doesn't reflect genuine price discovery. Ported from
# jun22-literal/final-v1 (2026-08-03/04) — same bug existed here unfixed.
_ALWAYS_24_7 = {"BTCUSDT", "BTCUSD"}


# Session anchor type:
#   int               → static UTC hour (e.g. 0 for BTC) — DST-naive, fine for 24/7 markets
#   (tz_name, hour)   → local hour in IANA timezone (e.g. ("America/New_York", 18))
#                       UTC offset is recomputed per-date so DST transitions are handled
SessionAnchor = int | tuple[str, int]


def _resolve_start_utc_dt(anchor: SessionAnchor, calendar_date: datetime) -> datetime:
    """Return a tz-aware UTC datetime for the session that anchors on `calendar_date`.

    For int anchor: returns `calendar_date` at `hour:00 UTC` (always the same offset).
    For (tz, hour) anchor: returns `calendar_date` at `hour:00 local_tz`, converted
    to UTC — so EDT/EST transitions automatically shift the UTC equivalent.
    """
    if isinstance(anchor, int):
        return calendar_date.replace(hour=anchor, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    tz_name, local_hour = anchor
    local_dt = calendar_date.replace(hour=local_hour, minute=0, second=0, microsecond=0, tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(timezone.utc)


def _normalize_anchor(raw) -> SessionAnchor:
    """Accept either an int or a dict {tz, hour} from settings.yaml. Returns SessionAnchor."""
    if isinstance(raw, dict):
        tz = raw.get("tz") or raw.get("timezone")
        hour = raw.get("hour")
        if tz is not None and hour is not None:
            return (str(tz), int(hour))
    return int(raw or 0)


def _session_day_key(ts: int, anchor) -> str:
    """Return the IST calendar-date label for whichever session is active at ts.

    Accepts anchor as int (UTC hour) / tuple (tz, hour) / dict {tz, hour} —
    normalized internally so all consumers can pass whatever form they have.

    Examples:
      BTCUSDT  (0):  UTC 23:30 May 19 → start UTC 00:00 May 19 → IST 05:30 May 19 → "2026-05-19"
      XAUTUSDT (NY 18, DST):  UTC 21:00 May 19 → start UTC 22:00 May 18 → IST 03:30 May 19 → "2026-05-19"
      XAUTUSDT (NY 18, EST):  UTC 21:00 Dec 19 → start UTC 23:00 Dec 18 → IST 04:30 Dec 19 → "2026-12-19"
    """
    a: SessionAnchor = _normalize_anchor(anchor) if not isinstance(anchor, (int, tuple)) else anchor
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    candidate = _resolve_start_utc_dt(a, utc_dt)
    if candidate > utc_dt:
        candidate = _resolve_start_utc_dt(a, utc_dt - timedelta(days=1))
    return candidate.astimezone(_IST).strftime("%Y-%m-%d")


def _week_key(ts: int) -> str:
    """ISO week key using IST date (consistent with _session_day_key for typical sessions)."""
    dt = datetime.fromtimestamp(ts, tz=_IST)
    return f"{dt.strftime('%G')}-W{dt.strftime('%V')}"


def _day_bounds(ist_date_str: str, anchor: "int | tuple[str, int] | dict | None" = 0) -> tuple[int, int]:
    """Return (start_ts, end_ts) for the session labeled by IST date `ist_date_str`.

    The IST label is always +1 calendar day from the session's UTC/local start
    (for any sane session that begins after IST midnight). So we anchor on
    (IST date - 1).

    BTCUSDT  (anchor=0):    IST "2026-05-19" → 2026-05-19 00:00 UTC → +24h
    XAUTUSDT (NY 18, EDT):  IST "2026-05-19" → NY  2026-05-18 18:00 EDT = 22:00 UTC → +24h
    XAUTUSDT (NY 18, EST):  IST "2026-12-19" → NY  2026-12-18 18:00 EST = 23:00 UTC → +24h
    """
    a: SessionAnchor = anchor if isinstance(anchor, (int, tuple)) else _normalize_anchor(anchor)
    ist_dt = datetime.strptime(ist_date_str, "%Y-%m-%d")
    # For UTC-midnight sessions (anchor=0), label = anchor date. Otherwise +1.
    if isinstance(a, int) and a == 0:
        anchor_date = ist_dt
    else:
        anchor_date = ist_dt - timedelta(days=1)
    start_dt = _resolve_start_utc_dt(a, anchor_date)
    start = int(start_dt.timestamp())
    return start, start + 86400


def _week_bounds(week_str: str, anchor: "int | tuple[str, int] | dict[str, object] | None" = 0) -> tuple[int, int]:
    """Return (start_ts, end_ts) for an ISO week string like '2026-W21'.

    Anchored at Monday of that ISO week. For non-UTC anchors (e.g. NY 18:00),
    we take Monday's *local* 18:00 and convert to UTC.
    """
    a: SessionAnchor = anchor if isinstance(anchor, (int, tuple)) else _normalize_anchor(anchor)
    year, week = week_str.split("-W")
    monday = datetime.strptime(f"{year}-W{week}-1", "%G-W%V-%u")
    start_dt = _resolve_start_utc_dt(a, monday)
    start = int(start_dt.timestamp())
    return start, start + 604800


# ── backwards-compat shim for legacy callers passing a plain UTC hour int ────
def _utc_session_date(ist_date_str: str, session_start_utc: int) -> str:
    """Kept for any old callers — returns the UTC calendar-date for the anchor."""
    start_ts, _ = _day_bounds(ist_date_str, session_start_utc)
    return datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")


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
    va_width = None
    if vp.vah is not None and vp.val is not None:
        va_width = round(vp.vah - vp.val, 4)
    return {
        "poc": vp.poc, "vah": vp.vah, "val": vp.val,
        "shape": vp.shape, "current_position": vp.current_position,
        "hvn_zones": vp.hvn_zones, "lvn_zones": vp.lvn_zones,
        "naked_poc": vp.naked_poc, "total_volume": vp.total_volume,
        "price_range": vp.price_range, "bar_count": vp.bar_count,
        "va_width": va_width,
    }


def _compute_period_vp(
    all_bars, start_ts: int, end_ts: int,
    bin_size: float | None = None,
) -> dict | None:
    from .volume_profile import compute
    period_bars = [b for b in all_bars if start_ts <= b.close_ts < end_ts]
    if len(period_bars) < _vp_min_bars():   # need at least vp_min_bars for cached VP
        return None
    latest_close = period_bars[-1].ohlc.c
    vp = compute(period_bars, "cached", latest_close, bin_size=bin_size)
    d = _vp_to_dict(vp)
    if d is not None:
        d["price_range_high"] = round(max(b.ohlc.h for b in period_bars), 4)
        d["price_range_low"] = round(min(b.ohlc.l for b in period_bars), 4)
    return d


def _anchor_to_storage(anchor: SessionAnchor) -> int | dict:
    """Serialize a SessionAnchor to a JSON-friendly form for the cache."""
    if isinstance(anchor, tuple):
        return {"tz": anchor[0], "hour": anchor[1]}
    return int(anchor)


def _shift_vp(vp: dict, offset: float) -> dict:
    """Return a copy of vp dict with all price levels shifted by offset."""
    if not offset:
        return vp
    shifted = dict(vp)
    for field in ("poc", "vah", "val"):
        if shifted.get(field) is not None:
            shifted[field] = round(shifted[field] + offset, 4)
    if shifted.get("naked_poc") is not None:
        shifted["naked_poc"] = round(shifted["naked_poc"] + offset, 4)
    if shifted.get("hvn_zones"):
        shifted["hvn_zones"] = [
            {"low": round(z["low"] + offset, 4), "high": round(z["high"] + offset, 4)}
            for z in shifted["hvn_zones"]
        ]
    if shifted.get("lvn_zones"):
        shifted["lvn_zones"] = [
            {"low": round(z["low"] + offset, 4), "high": round(z["high"] + offset, 4)}
            for z in shifted["lvn_zones"]
        ]
    return shifted


def _compute_venue_offset(symbol: str, broker_symbol: str, bybit_last_close: float) -> float:
    """Fetch Vantage mid and return (vantage_mid - bybit_close). Returns 0.0 on failure."""
    try:
        from execution.venue_translator import fetch_venue_quote
        quote = fetch_venue_quote("vantage_mt5", broker_symbol)
        if quote.get("ok") and quote.get("mid"):
            offset = float(quote["mid"]) - bybit_last_close
            LOG.info(f"[vp_cache] {symbol} venue offset: vantage_mid={quote['mid']:.2f} "
                     f"bybit_close={bybit_last_close:.2f} → offset={offset:+.2f}")
            return offset
        LOG.info(f"[vp_cache] {symbol} venue quote unavailable ({quote.get('error')}) — offset=0")
    except Exception as e:
        LOG.info(f"[vp_cache] {symbol} venue offset fetch failed: {e} — offset=0")
    return 0.0


def build_and_save(
    symbols: list[str],
    primary_tf: str = "1m",
    session_start_utc: dict | None = None,
    vp_bin_size: dict | None = None,
    venue_price_offset: dict | None = None,
    symbol_map: dict | None = None,
) -> None:
    """Pre-compute last 5 days + last 2 weeks VP for each symbol. Save to cache.

    session_start_utc:  {symbol: anchor} — int (DST-naive) or {tz, hour} (DST-aware).
    vp_bin_size:        {symbol: float} — tick-aligned bin width (e.g. {"XAUTUSDT": 0.5}).
    venue_price_offset: {symbol: float | "auto" | None} — shift all VP price levels to
                        match execution venue (e.g. Vantage XAUUSD+ vs ByBit XAUTUSDT).
                        "auto": compute from MT5 quote vs last ByBit close at build time.
    symbol_map:         {symbol: broker_symbol} — used when offset="auto" to fetch MT5 quote.
    """
    from pipeline.state_store import store as _store
    session_cfg = session_start_utc or {}
    bin_cfg = vp_bin_size or {}
    offset_cfg = venue_price_offset or {}
    sym_map = symbol_map or {}
    cache = _load()
    now_ts = int(time.time())

    for symbol in symbols:
        s = _store()
        all_bars = s.recent(symbol, primary_tf, 100_000)
        if not all_bars:
            LOG.info(f"[vp_cache] no bars for {symbol} {primary_tf} — skip")
            continue

        from typing import Any as _Any
        anchor: SessionAnchor = _normalize_anchor(session_cfg.get(symbol, 0))
        from .volume_profile import DEFAULT_BIN_SIZE as _DEFAULT_BIN_SIZE
        bin_size: float | None = (
            float(bin_cfg[symbol]) if symbol in bin_cfg
            else _DEFAULT_BIN_SIZE.get(symbol)  # tick-aligned fallback
        )
        cache.setdefault(symbol, {"daily": {}, "weekly": {}, "session_start_utc": _anchor_to_storage(anchor)})  # type: ignore[union-attr]
        sym_cache: dict[str, _Any] = cache[symbol]  # type: ignore[assignment]
        sym_cache["session_start_utc"] = _anchor_to_storage(anchor)

        # Venue price offset — compute or store
        raw_offset = offset_cfg.get(symbol)
        if raw_offset == "auto":
            broker_sym = sym_map.get(symbol, symbol)
            last_close = all_bars[-1].ohlc.c
            computed_offset = _compute_venue_offset(symbol, broker_sym, last_close)
            sym_cache["venue_price_offset"] = computed_offset
        elif raw_offset is not None:
            sym_cache["venue_price_offset"] = float(raw_offset)
        # else: leave existing offset in cache untouched (or absent = 0)
        if bin_size is not None:
            sym_cache["bin_size"] = bin_size
        computed = 0

        # 7 calendar days always contain exactly 5 weekdays (one full weekly cycle),
        # so this still yields exactly 5 trading-day periods for non-24/7 symbols
        # once weekends are excluded below.
        for days_back in range(7):
            ts_for_day = now_ts - days_back * 86400
            date_key = _session_day_key(ts_for_day, anchor)
            # Real gold is closed weekends; Binance's XAUUSDT perp keeps ticking, so a
            # Sat/Sun session would be built from contaminated volume. Skip it entirely
            # (crypto symbols trade 24/7 and are exempt).
            if symbol.upper() not in _ALWAYS_24_7 and datetime.strptime(date_key, "%Y-%m-%d").weekday() >= 5:
                sym_cache["daily"].pop(date_key, None)  # purge a stale pre-fix weekend entry, if any
                continue
            # Always recompute — a day cached mid-session (e.g. a restart during a feed
            # gap) freezes a partial profile forever otherwise; the store may hold more
            # real bars for that date by the time we start again.
            start_ts, end_ts = _day_bounds(date_key, anchor)
            vp = _compute_period_vp(all_bars, start_ts, min(end_ts, now_ts), bin_size=bin_size)
            if vp:
                sym_cache["daily"][date_key] = vp
                computed += 1

        for weeks_back in range(2):
            ts_for_week = now_ts - weeks_back * 604800
            week_key = _week_key(ts_for_week)
            if week_key in sym_cache["weekly"] and weeks_back > 0:
                continue
            start_ts, end_ts = _week_bounds(week_key, anchor)
            vp = _compute_period_vp(all_bars, start_ts, min(end_ts, now_ts), bin_size=bin_size)
            if vp:
                sym_cache["weekly"][week_key] = vp
                computed += 1

        d_count = len(sym_cache["daily"])  # type: ignore[arg-type]
        w_count = len(sym_cache["weekly"])  # type: ignore[arg-type]
        LOG.info(f"[vp_cache] {symbol}: computed {computed} new periods (daily: {d_count}, weekly: {w_count})")

    _save(cache)
    LOG.info(f"[vp_cache] saved → {CACHE_FILE}")


def refresh_today(symbol: str, primary_tf: str = "1m") -> bool:
    """Recompute only today's VP entry for one symbol. Fast path for 1m intraday refresh.

    Skips the full 5-day loop and the 100k-bar reload that build_and_save does.
    Returns True if the entry was updated, False if not enough bars (< vp_min_bars).
    """
    from pipeline.state_store import store as _store
    from .volume_profile import compute
    cache = _load()
    sym_cache = cache.get(symbol)
    if sym_cache is None:
        return False
    anchor: SessionAnchor = _normalize_anchor(sym_cache.get("session_start_utc") or 0)
    from .volume_profile import DEFAULT_BIN_SIZE as _DEFAULT_BIN_SIZE
    bin_size: float | None = (
        float(sym_cache["bin_size"]) if "bin_size" in sym_cache
        else _DEFAULT_BIN_SIZE.get(symbol)  # tick-aligned fallback
    )

    now_ts = int(time.time())
    date_key = _session_day_key(now_ts, anchor)
    start_ts, end_ts = _day_bounds(date_key, anchor)

    s = _store()
    today_bars = [b for b in s.recent(symbol, primary_tf, 1500)
                  if start_ts <= b.close_ts < min(end_ts, now_ts)]
    if len(today_bars) < _vp_min_bars():
        return False

    latest_close = today_bars[-1].ohlc.c
    vp = compute(today_bars, "cached", latest_close, bin_size=bin_size)
    if not vp:
        return False

    vp["price_range_high"] = round(max(b.ohlc.h for b in today_bars), 4)
    vp["price_range_low"]  = round(min(b.ohlc.l for b in today_bars), 4)
    sym_cache["daily"][date_key] = vp
    _save(cache)
    return True



def _get_offset(sym: dict) -> float:
    return float(sym.get("venue_price_offset") or 0.0)


def get(symbol: str, period: str) -> dict | None:
    """Get cached VP for symbol + period (today / this week), venue-offset applied."""
    cache = _load()
    sym = cache.get(symbol, {})
    now_ts = int(time.time())
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)
    if period == "daily":
        key = _session_day_key(now_ts, anchor)
        vp = sym.get("daily", {}).get(key)
    elif period == "weekly":
        key = _week_key(now_ts)
        vp = sym.get("weekly", {}).get(key)
    else:
        return None
    return _shift_vp(vp, offset) if vp else None


def _prev_trading_day_key(now_ts: int, anchor: SessionAnchor) -> str:
    """Session day key for the most recent REAL trading day before today — walks back
    past Saturday/Sunday. The real gold market is closed weekends; Binance's XAUT feed
    keeps posting (thin, crypto-venue) bars anyway, so a literal "yesterday" can land on
    a Sat/Sun session with no genuine structure. Same skip rule as period_profiles_session."""
    for i in range(1, 8):
        k = _session_day_key(now_ts - i * 86400, anchor)
        if datetime.strptime(k, "%Y-%m-%d").weekday() < 5:   # 0-4 = Mon-Fri
            return k
    return _session_day_key(now_ts - 86400, anchor)   # unreachable in practice


def get_prev_and_today(symbol: str) -> tuple[dict | None, dict | None]:
    """Return (prev_day_vp, today_vp) for daily period, both venue-offset applied.

    Used by /exec/zones to overlay both periods: prev-D completed profile and the
    forming today session. Either may be None if not yet cached. prev-D skips
    Saturday/Sunday (see _prev_trading_day_key).
    """
    cache = _load()
    sym = cache.get(symbol, {})
    now_ts = int(time.time())
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)

    today_key = _session_day_key(now_ts, anchor)
    prev_key = _prev_trading_day_key(now_ts, anchor)
    daily = sym.get("daily", {})

    today_vp = _shift_vp(daily[today_key], offset) if today_key in daily else None
    prev_vp = _shift_vp(daily[prev_key], offset) if prev_key in daily else None
    return prev_vp, today_vp


def get_history(symbol: str, period: str, n: int = 5) -> list[dict]:
    """Get last N cached VP snapshots (newest last), venue-offset applied."""
    cache = _load()
    sym_cache = cache.get(symbol, {})
    offset = _get_offset(sym_cache)
    entries = sym_cache.get(period, {})
    sorted_keys = sorted(entries.keys())[-n:]
    return [{"period_key": k, **_shift_vp(entries[k], offset)} for k in sorted_keys]


def poc_sequence(symbol: str, period: str, n: int = 5) -> list[float | None]:
    return [e.get("poc") for e in get_history(symbol, period, n)]


_PROFILE_CFG_CACHE: dict | None = None


def _profile_draw_cfg() -> dict:
    """DRAWN-histogram-only config (vp_profile_bin per-symbol, vp_profile_smooth scalar).
    Read once from settings.grid_levels; decoupled from detection/grid bins."""
    global _PROFILE_CFG_CACHE
    if _PROFILE_CFG_CACHE is None:
        cfg = {"bin": {}, "smooth": 0.0, "points": 150, "days": 2}
        try:
            import yaml as _yaml
            import pathlib as _pl
            _s = _yaml.safe_load(
                (_pl.Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml").read_text()
            ) or {}
            # vp_profile_bin / vp_profile_smooth / vp_profile_points live under the vp_cache
            # section (next to vp_bin_size), NOT grid_levels (detection's vp_smooth_price).
            _g = _s.get("vp_cache") or {}
            cfg["bin"] = _g.get("vp_profile_bin") or {}
            cfg["smooth"] = float(_g.get("vp_profile_smooth", 0.0) or 0.0)
            cfg["points"] = int(_g.get("vp_profile_points", 150) or 150)
            cfg["days"] = int(_g.get("vp_profile_days", 2) or 2)
        except Exception:
            pass
        _PROFILE_CFG_CACHE = cfg
    return _PROFILE_CFG_CACHE


def _profile_from_bars(bars: list, draw_cfg: dict, draw_bin, offset: float) -> dict | None:
    """Build a venue-offset VP histogram dict from a pre-filtered bar list.

    Returns {"bin": float, "profile": [{price, vol}, ...]} or None if too few bars."""
    from .volume_profile import _build_bins
    import numpy as _np
    if len(bars) < _vp_min_bars():
        return None
    res = _build_bins(bars, bin_size=draw_bin)
    if not res:
        return None
    bins, centers, bin_size, _pmin, _pmax = res
    _raw = _np.asarray(bins, dtype=float)
    _nz = _np.nonzero(_raw > 0)[0]
    if len(_nz) == 0:
        return None
    _lo, _hi = int(_nz[0]), int(_nz[-1])
    _smooth_px = float(draw_cfg.get("smooth") or 0.0)
    _pts = int(draw_cfg.get("points") or 150)
    if _smooth_px > 0 and bin_size > 0:
        from scipy.ndimage import gaussian_filter1d as _gauss
        _raw = _gauss(_raw, sigma=max(0.5, _smooth_px / bin_size))
    _active = _hi - _lo + 1
    _step = max(1, _active // _pts) if _pts > 0 else 1
    profile = []
    for i in range(_lo, _hi + 1, _step):
        j = min(i + _step, _hi + 1)
        vol = float(_raw[i:j].sum())
        if vol <= 0:
            continue
        price = float(_np.mean(centers[i:j])) + offset
        profile.append({"price": round(price, 5), "vol": round(vol, 2)})
    return {"bin": round(float(bin_size) * _step, 5), "profile": profile}


def period_profile(symbol: str, period: str) -> dict | None:
    """Per-price volume histogram for the symbol's CURRENT period, venue-offset applied.

    The cache stores only aggregates (POC/zones/etc), so the bins are rebuilt here from
    stored 1m bars over the same session window get() uses — for the EA's VP overlay.
    Returns {"bin": <width>, "profile": [{price, vol}, ...]} (vol>0 bins, venue-shifted),
    or None if < vp_min_bars (matches _compute_period_vp's floor)."""
    from pipeline.state_store import store as _store
    cache = _load()
    sym = cache.get(symbol, {})
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)
    bin_cfg = sym.get("bin_size")
    now_ts = int(time.time())
    if period == "daily":
        ist_now = datetime.fromtimestamp(now_ts, tz=_IST)
        if ist_now.hour < 20:
            day_key = _session_day_key(now_ts - 86400, anchor)
        else:
            day_key = _session_day_key(now_ts, anchor)
        st, en = _day_bounds(day_key, anchor)
    elif period == "weekly":
        st, en = _week_bounds(_week_key(now_ts), anchor)
    else:
        return None
    end = min(en, now_ts)
    bars = [b for b in _store().recent(symbol, "1m", 100_000) if st <= b.close_ts < end]
    _draw_cfg = _profile_draw_cfg()
    _draw_bin = float((_draw_cfg["bin"] or {}).get(symbol, 0.0) or 0.0) or (float(bin_cfg) if bin_cfg else None)
    return _profile_from_bars(bars, _draw_cfg, _draw_bin, offset)


def period_profiles_session(symbol: str) -> list[dict]:
    """Return VP histograms for the last `vp_profile_days` sessions plus today's
    forming (rolling) session, each anchored at its own session start.

    Returns a list (oldest first) of {"bin", "profile", "start_ts"} dicts suitable for
    the EA's session-anchored VP drawing. Entries are omitted if < vp_min_bars are available,
    so thin/closed days simply drop out."""
    from pipeline.state_store import store as _store
    cache = _load()
    sym = cache.get(symbol, {})
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)
    bin_cfg = sym.get("bin_size")
    now_ts = int(time.time())
    _draw_cfg = _profile_draw_cfg()
    _draw_bin = float((_draw_cfg["bin"] or {}).get(symbol, 0.0) or 0.0) or (float(bin_cfg) if bin_cfg else None)
    _days = int(_draw_cfg.get("days") or 2)

    # oldest → newest, ending at today's forming session (day 0 = rolling).
    # Sat/Sun session days are SKIPPED (thin crypto-venue weekend trade — not structural);
    # walk back extra calendar days until `_days` trading days are collected.
    day_keys = []
    i = 0
    while len(day_keys) < _days and i < _days + 6:
        k = _session_day_key(now_ts - i * 86400, anchor)
        i += 1
        if datetime.strptime(k, "%Y-%m-%d").weekday() >= 5:   # 5=Sat 6=Sun
            continue
        if k not in day_keys:
            day_keys.append(k)
    day_keys.reverse()

    all_bars = _store().recent(symbol, "1m", 100_000)
    results = []
    for day_key in day_keys:
        st, en = _day_bounds(day_key, anchor)
        end = min(en, now_ts)
        bars = [b for b in all_bars if st <= b.close_ts < end]
        prof = _profile_from_bars(bars, _draw_cfg, _draw_bin, offset)
        if prof:
            prof["start_ts"] = st
            results.append(prof)
    return results


def rolling_profile(symbol: str, minutes: int = 1440) -> dict | None:
    """Trailing-window VP histogram (default 24h of 1m bars) — the same window the
    grid trigger's rolling VP (`_rolling_hvn`) sees, for chart parity. Venue-offset
    applied; start_ts = first bar of the window. None if < vp_min_bars."""
    from pipeline.state_store import store as _store
    cache = _load()
    sym = cache.get(symbol, {})
    offset = _get_offset(sym)
    bin_cfg = sym.get("bin_size")
    _draw_cfg = _profile_draw_cfg()
    _draw_bin = float((_draw_cfg["bin"] or {}).get(symbol, 0.0) or 0.0) or (float(bin_cfg) if bin_cfg else None)
    bars = _store().recent(symbol, "1m", minutes)
    prof = _profile_from_bars(bars, _draw_cfg, _draw_bin, offset)
    if prof:
        prof["start_ts"] = int(bars[0].close_ts)
    return prof


def get_va_regime(
    symbol: str,
    period: str = "daily",
    n: int = 3,
) -> dict:
    """Classify VA width trend across last N sessions.

    Returns:
        {
          "regime":    "expanding" | "contracting" | "neutral",
          "widths":    [float, ...],   # va_width per session, oldest first
          "pct_change": float,         # (newest - oldest) / oldest × 100
          "confidence": float,         # 0.0 – 1.0 (monotonic trend = higher)
        }

    Expanding VA = trend developing (breakout bias).
    Contracting VA = compression → mean-reversion to POC bias.
    Neutral = no clear direction.

    Uses va_width stored in each cached VP snapshot.
    Falls back to (vah - val) if va_width key absent (old cache entries).
    """
    snaps = get_history(symbol, period, n=n)
    widths = []
    for s in snaps:
        w = s.get("va_width")
        if w is None:
            vah, val = s.get("vah"), s.get("val")
            if vah is not None and val is not None:
                w = round(vah - val, 4)
        if w is not None and w > 0:
            widths.append(w)

    if len(widths) < 2:
        return {"regime": "neutral", "widths": widths, "pct_change": 0.0, "confidence": 0.0}

    oldest, newest = widths[0], widths[-1]
    pct_change = round((newest - oldest) / oldest * 100, 2) if oldest else 0.0

    # Monotonicity: all steps same direction = high confidence
    steps = [widths[i + 1] - widths[i] for i in range(len(widths) - 1)]
    all_up   = all(s > 0 for s in steps)
    all_down = all(s < 0 for s in steps)
    monotonic = all_up or all_down
    confidence = round(min(0.90, abs(pct_change) / 20 * (1.3 if monotonic else 1.0)), 2)

    if pct_change >= 5.0:
        regime = "expanding"
    elif pct_change <= -5.0:
        regime = "contracting"
    else:
        regime = "neutral"

    return {
        "regime":     regime,
        "widths":     widths,
        "pct_change": pct_change,
        "confidence": confidence,
    }
