"""Assemble the (cached_prefix, variable_suffix) prompt pair.

Cached prefix = system prompt + rules + few-shot examples → marked
cache_control by llm/client.py.

Variable suffix = recent bars + features for the current symbol/TF, JSON-encoded.
This is the only thing that changes call-to-call.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.footprint import FootprintMatrix, build as build_fp
from pipeline.features import (
    bar_delta,
    cumulative_delta,
    imbalance_per_level,
    stacked_imbalances,
    detect_absorption,
    poc,
    value_area,
)
from pipeline.features.volume_profile import from_store as vp_from_store
from pipeline.features.vp_history import get_history as vp_history
from pipeline.features import poc as poc_feat
import pipeline.features.vp_cache as vp_cache
from pipeline.features.session import current_session, htf_bias
from pipeline.types import Bar

PROMPTS_DIR = Path(__file__).resolve().parent


def active_version() -> str:
    return (PROMPTS_DIR / "system" / "current.txt").read_text().strip()


def _few_shot_block(n: int) -> str:
    p = PROMPTS_DIR / "few_shot" / "examples.jsonl"
    if not p.exists():
        return ""
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()][:n]
    return "\n\nExamples:\n" + "\n".join(lines)


def cached_prefix(few_shot_count: int = 4) -> str:
    version = active_version()
    system_text = (PROMPTS_DIR / "system" / f"{version}.txt").read_text()
    return system_text + _few_shot_block(few_shot_count)


def _bar_summary(bar: Bar) -> dict:
    fp = build_fp(bar)
    # value_area omitted from 1m bars — too narrow to be meaningful (~10pts)
    # Only include stacked imbalances with 2+ levels to reduce noise
    stacked = [z for z in stacked_imbalances(fp, min_stack=2)
               if z.count >= 2]
    return {
        "bar_id": bar.bar_id,
        "tf": bar.tf,
        "close_ts": bar.close_ts,
        "ohlc": {"o": bar.ohlc.o, "h": bar.ohlc.h, "l": bar.ohlc.l, "c": bar.ohlc.c},
        "delta": bar_delta(fp),
        "poc": poc(fp),
        "stacked": [
            {"side": z.side, "low": z.price_low, "high": z.price_high, "count": z.count}
            for z in stacked
        ],
        "absorption": [
            {"side": a.side, "price": a.price, "vol": a.volume, "pct": round(a.bar_pct, 3)}
            for a in detect_absorption(bar, fp)
        ],
    }


def _vp_summary(vp) -> dict | None:
    if vp is None:
        return None
    return {
        "poc": vp.poc,
        "vah": vp.vah,
        "val": vp.val,
        "shape": vp.shape,
        "current_position": vp.current_position,
        "hvn_zones": vp.hvn_zones[:5],  # top 5 only to keep token count low
        "lvn_zones": vp.lvn_zones[:5],
        "naked_poc": vp.naked_poc,
        "bar_count": vp.bar_count,
    }


def variable_suffix(
    primary: list[Bar],
    higher_tfs: dict[str, Bar | None] | None = None,
) -> str:
    """Encode recent primary-TF bars + higher-TF as-of context as JSON."""
    if not primary:
        return json.dumps({})
    symbol = primary[0].symbol
    primary_tf = primary[0].tf
    fps = [build_fp(b) for b in primary]
    cvd_5 = cumulative_delta(fps[-5:]) if len(fps) >= 5 else cumulative_delta(fps)

    # Session CVD — delta from instrument-specific session open (not midnight UTC)
    import time as _time
    from pipeline.state_store import store as _store
    from pipeline.features.vp_cache import (
        _session_day_key as _sdk, _day_bounds as _db, _load as _cache_load
    )
    _cache = _cache_load()
    _sess_utc: int = int((_cache.get(symbol, {}).get("session_start_utc") or 0))
    _now_ts = int(_time.time())
    _sess_day = _sdk(_now_ts, _sess_utc)
    _sess_start, _ = _db(_sess_day, _sess_utc)
    session_bars = [b for b in _store().recent(symbol, primary_tf, 2000)
                    if b.close_ts >= _sess_start]
    session_cvd = round(cumulative_delta([build_fp(b) for b in session_bars]), 4) if session_bars else 0.0

    # Volume profiles — current session + prior day (PD) for reference levels
    cached_daily  = vp_cache.get(symbol, "daily")
    cached_weekly = vp_cache.get(symbol, "weekly")
    daily_vp  = cached_daily  if cached_daily  else _vp_summary(vp_from_store(symbol, primary_tf, "daily"))
    weekly_vp = cached_weekly if cached_weekly else _vp_summary(vp_from_store(symbol, primary_tf, "weekly"))

    # Prior Day VP — yesterday's completed session (always a fixed reference)
    _hist = vp_cache.get_history(symbol, "daily", n=2)
    prior_day_vp = None
    if len(_hist) >= 2:
        _pd = _hist[-2]   # second-most-recent = prior completed day
        prior_day_vp = {k: _pd.get(k) for k in
                        ["poc","vah","val","shape","hvn_zones","lvn_zones","naked_poc","bar_count"]}

    # VP history — last 5 daily POCs + last 2 weekly summaries
    daily_poc_seq = vp_cache.poc_sequence(symbol, "daily", n=5)
    weekly_hist = [
        {"period_key": e["period_key"], "poc": e.get("poc"), "shape": e.get("shape"),
         "vah": e.get("vah"), "val": e.get("val")}
        for e in vp_cache.get_history(symbol, "weekly", n=2)
    ]

    # Session + HTF bias
    latest_ts = primary[-1].close_ts
    sess = current_session(latest_ts, symbol=symbol)

    from pipeline.state_store import store as _store
    daily_bars = _store().recent(symbol, "1d", 30) or []
    htf = htf_bias(daily_bars) if daily_bars else {"sma_20": None, "close": None, "bias": "neutral"}

    # Last 3 decisions for this symbol — show Claude its recent history
    from pathlib import Path as _Path
    _dlog = _Path(__file__).resolve().parent.parent / "data" / "decisions.jsonl"
    recent_decisions: list[dict] = []
    if _dlog.exists():
        rows = [json.loads(l) for l in _dlog.read_text().splitlines() if l.strip()]
        sym_rows = [r for r in rows if r.get("symbol") == symbol][-3:]
        recent_decisions = [
            {"side": r["decision"]["side"],
             "confidence": r["decision"]["confidence"],
             "rationale": r["decision"].get("rationale", "")[:80],
             "bar_id": r.get("bar_id", "")}
            for r in sym_rows
        ]

    # --- New market structure features ---
    current_price = primary[-1].ohlc.c

    # Day type classifier
    day_type_ctx: dict | None = None
    try:
        from pipeline.features.day_type import classify as _day_classify
        from types import SimpleNamespace as _NS
        _daily_vp_ns = _NS(**cached_daily) if cached_daily else None
        _dt = _day_classify(session_bars, daily_vp=_daily_vp_ns)
        day_type_ctx = {
            "type": _dt.type,
            "confidence": _dt.confidence,
            "ib_expansion_pct": _dt.ib_expansion_pct,
            "max_legs": _dt.max_legs,
            "grid_mode": _dt.grid_mode,
            "reason": _dt.reason,
        }
    except Exception:
        pass

    # Auction failure / LVN transit
    auction_ctx: dict | None = None
    try:
        from pipeline.features.auction import detect as _auction_detect
        _lvn_zones = (cached_daily or {}).get("lvn_zones", [])
        _avg_bar_vol = sum(
            sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)
            for b in primary
        ) / max(len(primary), 1)
        _asig = _auction_detect(primary, _lvn_zones, session_avg_vol=_avg_bar_vol)
        if _asig.type != "none":
            auction_ctx = {
                "type": _asig.type,
                "zone_low": _asig.zone_low,
                "zone_high": _asig.zone_high,
                "confidence": _asig.confidence,
                "avg_vol_ratio": _asig.avg_vol_ratio,
                "reason": _asig.reason,
            }
    except Exception:
        pass

    # Delta divergence
    divergence_ctx: dict | None = None
    try:
        from pipeline.features.delta_divergence import detect as _div_detect
        _dsig = _div_detect(primary, n=5)
        if _dsig.type != "none":
            divergence_ctx = {
                "type": _dsig.type,
                "confidence": _dsig.confidence,
                "price_change_pct": _dsig.price_change_pct,
                "reason": _dsig.reason,
            }
    except Exception:
        pass

    # Wave phase + Fibonacci levels
    wave_ctx: dict | None = None
    try:
        from pipeline.features.wave import classify as _wave_classify, target_tier as _tier
        _wp = _wave_classify(primary, lookback=min(len(primary), 20))
        if _wp.phase != "unknown":
            _tier_name, _tier_price = _tier(_wp, current_price)
            wave_ctx = {
                "phase": _wp.phase,
                "direction": _wp.direction,
                "confidence": _wp.confidence,
                "retrace_pct": _wp.retrace_pct,
                "cvd_quality": _wp.cvd_quality,
                "fib_retrace": _wp.fib_retrace,
                "invalidation": _wp.invalidation,
                "target_tier": _tier_name,
                "target_price": _tier_price,
                "reason": _wp.reason,
            }
    except Exception:
        pass

    # Sweep detection (uses cached swing points)
    sweep_ctx: dict | None = None
    try:
        from pipeline.features.swing import get as _get_swing
        from pipeline.features.sweep import detect as _sweep_detect
        _sp = _get_swing(symbol)
        if _sp and primary:
            _sw = _sweep_detect(primary[-1], _sp, prev_bars=primary[:-1])
            if _sw.type != "none":
                sweep_ctx = {
                    "type": _sw.type,
                    "swept_level": _sw.swept_level,
                    "level_label": _sw.level_label,
                    "confidence": _sw.confidence,
                    "reason": _sw.reason,
                }
    except Exception:
        pass

    # Big trade session memory
    big_trade_ctx: dict | None = None
    try:
        from pipeline.features.big_trade import session_summary as _bt_summary
        big_trade_ctx = _bt_summary(symbol, current_price)
    except Exception:
        pass

    payload: dict = {
        "symbol": symbol,
        "recent_decisions": recent_decisions,
        "session": {
            "name": sess.session,
            "in_active_hours": sess.in_active_hours,
            "utc_hour": sess.utc_hour,
        },
        "htf_bias": htf,
        "primary_tf_bars": [_bar_summary(b) for b in primary],
        "cumulative_delta": cumulative_delta(fps),
        "cvd_5bar": round(cvd_5, 4),
        "session_cvd": session_cvd,
        "cvd_trend": "bearish" if session_cvd < -20 else "bullish" if session_cvd > 20 else "neutral",
        "higher_tf": {
            tf: (_bar_summary(b) if b else None)
            for tf, b in (higher_tfs or {}).items()
        },
        "daily_vp": daily_vp,
        "prior_day_vp": prior_day_vp,
        "weekly_vp": weekly_vp,
        "vp_context": {
            "daily_poc_last5": daily_poc_seq,
            "weekly_last2": weekly_hist,
        },
        "market_structure": {
            "day_type": day_type_ctx,
            "auction": auction_ctx,
            "divergence": divergence_ctx,
            "wave": wave_ctx,
            "sweep": sweep_ctx,
            "big_trade": big_trade_ctx,
        },
    }
    return json.dumps(payload)
