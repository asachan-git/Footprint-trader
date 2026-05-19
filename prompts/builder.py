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

    # Session CVD — delta from midnight UTC today (full-day running total)
    import time as _time
    midnight_ts = int(_time.time()) // 86400 * 86400
    from pipeline.state_store import store as _store
    session_bars = [b for b in _store().recent(symbol, primary_tf, 2000)
                    if b.close_ts >= midnight_ts]
    session_cvd = round(cumulative_delta([build_fp(b) for b in session_bars]), 4) if session_bars else 0.0

    # Volume profiles — try cache first (pre-computed), fall back to live compute
    cached_daily = vp_cache.get(symbol, "daily")
    cached_weekly = vp_cache.get(symbol, "weekly")
    daily_vp = cached_daily if cached_daily else _vp_summary(vp_from_store(symbol, primary_tf, "daily"))
    weekly_vp = cached_weekly if cached_weekly else _vp_summary(vp_from_store(symbol, primary_tf, "weekly"))

    # VP history — last 5 daily POCs from cache, last 2 weekly from cache
    daily_poc_seq = vp_cache.poc_sequence(symbol, "daily", n=5)
    weekly_hist = [
        {"period_key": e["period_key"], "poc": e.get("poc"), "shape": e.get("shape"),
         "vah": e.get("vah"), "val": e.get("val")}
        for e in vp_cache.get_history(symbol, "weekly", n=2)
    ]

    # Session + HTF bias
    latest_ts = primary[-1].close_ts
    sess = current_session(latest_ts)

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
        "weekly_vp": weekly_vp,
        "vp_context": {
            "daily_poc_last5": daily_poc_seq,
            "weekly_last2": weekly_hist,
        },
    }
    return json.dumps(payload)
