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
CACHE_FILE = ROOT / "data" / "vp_cache.json"


_IST = timezone(timedelta(hours=5, minutes=30))

# Symbols that legitimately trade through the weekend — everything else (gold via
# Binance's XAUUSDT perp) is exempted from Sat/Sun VP builds since the real market
# is closed and that volume doesn't reflect genuine price discovery.
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
    if len(period_bars) < 30:   # need at least 30 bars for cached VP
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


_VENUE_QUOTE_MAX_AGE_S = 300.0


def _compute_venue_offset(symbol: str, broker_symbol: str,
                          bybit_last_close: float) -> float | None:
    """(venue_mid − analysis_close), or None when it cannot be determined.

    Returning None rather than 0.0 is deliberate — see the caller. A failed fetch means
    "unknown", and overwriting a known-good offset with 0 silently re-introduces the entire
    Binance↔Vantage basis error.

    Source order (2026-08-05):
      1. The EA's own bid/ask, cached by ExecBridge on every /exec/poll. This is the exact
         price orders execute against and needs no external service. PRIMARY.
      2. venue_translator / MetaAPI — the original path. It has been failing on this branch
         the whole time with 'METAAPI_TOKEN' (this branch drives MT5 over the EA/rpyc bridge
         and holds no MetaAPI credentials), which is why the stored offset sat at 0.0 while
         the real basis was ~5.5pt. Kept only for deployments that do have MetaAPI.
    """
    # 1) EA-reported quote (primary)
    try:
        from execution.exec_bridge import ExecBridge
        best = None
        with ExecBridge._lock:
            for (_acct, _sym), q in ExecBridge._quotes.items():
                if _sym != broker_symbol or not q.get("mid"):
                    continue
                if best is None or float(q.get("ts", 0) or 0) > float(best.get("ts", 0) or 0):
                    best = dict(q)
        if best is not None:
            age = time.time() - float(best.get("ts", 0) or 0)
            if age <= _VENUE_QUOTE_MAX_AGE_S:
                offset = float(best["mid"]) - bybit_last_close
                LOG.info(f"[vp_cache] {symbol} venue offset (EA quote): "
                         f"venue_mid={float(best['mid']):.2f} "
                         f"analysis_close={bybit_last_close:.2f} → offset={offset:+.2f}")
                return offset
            LOG.info(f"[vp_cache] {symbol} EA quote stale ({age:.0f}s) — trying fallback")
    except Exception as e:
        LOG.info(f"[vp_cache] {symbol} EA quote unavailable ({e}) — trying fallback")

    # 2) MetaAPI (legacy fallback)
    try:
        from execution.venue_translator import fetch_venue_quote
        quote = fetch_venue_quote("vantage_mt5", broker_symbol)
        if quote.get("ok") and quote.get("mid"):
            offset = float(quote["mid"]) - bybit_last_close
            LOG.info(f"[vp_cache] {symbol} venue offset (metaapi): vantage_mid={quote['mid']:.2f} "
                     f"analysis_close={bybit_last_close:.2f} → offset={offset:+.2f}")
            return offset
        LOG.info(f"[vp_cache] {symbol} venue quote unavailable ({quote.get('error')}) "
                 f"— KEEPING existing offset")
    except Exception as e:
        LOG.info(f"[vp_cache] {symbol} venue offset fetch failed: {e} — KEEPING existing offset")
    return None


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
        bin_size: float | None = float(bin_cfg[symbol]) if symbol in bin_cfg else None
        cache.setdefault(symbol, {"daily": {}, "weekly": {}, "session_start_utc": _anchor_to_storage(anchor)})  # type: ignore[union-attr]
        sym_cache: dict[str, _Any] = cache[symbol]  # type: ignore[assignment]
        sym_cache["session_start_utc"] = _anchor_to_storage(anchor)

        # Venue price offset — compute or store
        raw_offset = offset_cfg.get(symbol)
        if raw_offset == "auto":
            broker_sym = sym_map.get(symbol, symbol)
            last_close = all_bars[-1].ohlc.c
            computed_offset = _compute_venue_offset(symbol, broker_sym, last_close)
            if computed_offset is not None:
                sym_cache["venue_price_offset"] = computed_offset
            else:
                # STICKY (2026-08-05): keep the last known-good offset instead of resetting
                # to 0. The build runs at startup (before the first EA poll, so no quote
                # yet) and again on every bar via routes/ingest.py — a transient miss must
                # not wipe a good value and silently un-shift every zone by the full basis.
                _kept = float(sym_cache.get("venue_price_offset") or 0.0)
                LOG.info(f"[vp_cache] {symbol} offset not resolvable now — keeping {_kept:+.2f}")
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


def _get_offset(sym: dict) -> float:
    return float(sym.get("venue_price_offset") or 0.0)


_offset_memo: dict[str, tuple[float, float]] = {}   # symbol → (offset, read_ts)
_OFFSET_MEMO_TTL_S = 10.0


def venue_offset(symbol: str) -> float:
    """Public accessor for the stored (venue − analysis) additive offset.

    For code that builds price levels OUTSIDE the cached VP (e.g. the rolling-window VP in
    zone_triggers) and therefore has to apply the same shift by hand — otherwise those
    levels stay in ANALYSIS frame while everything from get() is in VENUE frame, and
    merging the two silently mixes frames.

    Memoised for _OFFSET_MEMO_TTL_S because _load() re-reads and re-parses the whole cache
    file on every call, and this is hit on the ~1s poll path. The offset only changes on a
    VP rebuild (every intraday_refresh_bars minutes), so a few seconds of staleness is free.
    """
    now = time.time()
    hit = _offset_memo.get(symbol)
    if hit is not None and (now - hit[1]) < _OFFSET_MEMO_TTL_S:
        return hit[0]
    try:
        off = _get_offset(_load().get(symbol, {}) or {})
    except Exception:
        off = hit[0] if hit else 0.0
    _offset_memo[symbol] = (off, now)
    return off


def get(symbol: str, period: str) -> dict | None:
    """Get cached VP for symbol + period (today / this week), in ANALYSIS frame.

    NO venue shift is applied here (changed 2026-08-06). The VP is built from analysis
    (Binance) bars and every detector that reads it — _cached_hvn, _t_hvn_edge, _t_va,
    _prior_day_hvn, _outside_daily_va, and the `daily_vp` plan_grid_levels threads through
    for TPs/POC-reversion — operates in analysis frame, because plan_grid_levels performs
    the SINGLE venue rebase at the end (_rebase_to_venue, the 2026-07-14 design).

    This function used to apply `_shift_vp(vp, offset)`. That was harmless only while the
    stored offset was stuck at 0.0; once it began resolving (~-5pt) every detector started
    comparing venue-frame zones against analysis-frame prices. Live symptom: price tapped
    an HVN top edge (venue 4249.53 vs edge 4249.30) while touch_arm_trigger compared
    4254.20 and saw nothing — HVN touches silently stopped arming.

    Venue-frame consumers (the /exec/zones chart payload) apply `venue_offset()` themselves.
    """
    cache = _load()
    sym = cache.get(symbol, {})
    now_ts = int(time.time())
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    if period == "daily":
        key = _session_day_key(now_ts, anchor)
        vp = sym.get("daily", {}).get(key)
    elif period == "weekly":
        key = _week_key(now_ts)
        vp = sym.get("weekly", {}).get(key)
    else:
        return None
    return dict(vp) if vp else None


def get_history(symbol: str, period: str, n: int = 5) -> list[dict]:
    """Last N cached VP snapshots (newest last), ANALYSIS frame — see get()."""
    cache = _load()
    sym_cache = cache.get(symbol, {})
    entries = sym_cache.get(period, {})
    sorted_keys = sorted(entries.keys())[-n:]
    return [{"period_key": k, **entries[k]} for k in sorted_keys]


def poc_sequence(symbol: str, period: str, n: int = 5) -> list[float | None]:
    return [e.get("poc") for e in get_history(symbol, period, n)]


def period_profile(symbol: str, period: str) -> dict | None:
    """Per-price volume histogram for the symbol's CURRENT period, venue-offset applied.

    The cache stores only aggregates (POC/zones/etc), so the bins are rebuilt here from
    stored 1m bars over the same session window get() uses — for the EA's VP overlay.
    Returns {"bin": <width>, "profile": [{price, vol}, ...]} (vol>0 bins, venue-shifted),
    or None if <30 bars (matches _compute_period_vp's floor)."""
    from pipeline.state_store import store as _store
    from .volume_profile import _build_bins
    cache = _load()
    sym = cache.get(symbol, {})
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)
    bin_cfg = sym.get("bin_size")
    now_ts = int(time.time())
    if period == "daily":
        st, en = _day_bounds(_session_day_key(now_ts, anchor), anchor)
    elif period == "weekly":
        st, en = _week_bounds(_week_key(now_ts), anchor)
    else:
        return None
    end = min(en, now_ts)
    bars = [b for b in _store().recent(symbol, "1m", 100_000) if st <= b.close_ts < end]
    if len(bars) < 30:
        return None
    res = _build_bins(bars, bin_size=float(bin_cfg) if bin_cfg else None)
    if not res:
        return None
    bins, centers, bin_size, _pmin, _pmax = res
    profile = [{"price": round(float(centers[i]) + offset, 5), "vol": round(float(bins[i]), 2)}
               for i in range(len(bins)) if bins[i] > 0]
    return {"bin": round(float(bin_size), 5), "profile": profile}


def period_profiles_session(symbol: str, days: int = 5) -> list[dict]:
    """VP histograms for the last `days` trading sessions (oldest first), each with
    its own {"bin", "profile", "start_ts"} — for the EA to draw one histogram per
    day, anchored at that day's own session start. Sat/Sun sessions are skipped for
    non-24/7 symbols (mirrors build_and_save's weekend exclusion)."""
    from pipeline.state_store import store as _store
    from .volume_profile import _build_bins
    cache = _load()
    sym = cache.get(symbol, {})
    anchor: SessionAnchor = _normalize_anchor(sym.get("session_start_utc") or 0)
    offset = _get_offset(sym)
    bin_cfg = sym.get("bin_size")
    now_ts = int(time.time())

    day_keys: list[str] = []
    i = 0
    while len(day_keys) < days and i < days + 6:
        k = _session_day_key(now_ts - i * 86400, anchor)
        i += 1
        if symbol.upper() not in _ALWAYS_24_7 and datetime.strptime(k, "%Y-%m-%d").weekday() >= 5:
            continue
        if k not in day_keys:
            day_keys.append(k)
    day_keys.reverse()

    all_bars = _store().recent(symbol, "1m", 100_000)
    results: list[dict] = []
    for day_key in day_keys:
        st, en = _day_bounds(day_key, anchor)
        end = min(en, now_ts)
        bars = [b for b in all_bars if st <= b.close_ts < end]
        if len(bars) < 30:
            continue
        res = _build_bins(bars, bin_size=float(bin_cfg) if bin_cfg else None)
        if not res:
            continue
        bins, centers, bin_size, _pmin, _pmax = res
        profile = [{"price": round(float(centers[i2]) + offset, 5), "vol": round(float(bins[i2]), 2)}
                   for i2 in range(len(bins)) if bins[i2] > 0]
        results.append({"bin": round(float(bin_size), 5), "profile": profile, "start_ts": st})
    return results


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
