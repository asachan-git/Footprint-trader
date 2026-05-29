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
)
from pipeline.features.volume_profile import from_store as vp_from_store
from pipeline.features.vp_history import get_history as vp_history
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
    raw_lines = [ln for ln in p.read_text().splitlines() if ln.strip()][:n]
    parts: list[str] = []
    for ln in raw_lines:
        try:
            ex = json.loads(ln)
            if "input" in ex and "output" in ex:
                parts.append(f"Input:\n{ex['input']}\nOutput:\n{json.dumps(ex['output'])}")
            else:
                parts.append(ln)
        except Exception:
            parts.append(ln)
    return "\n\n[FEW-SHOT EXAMPLES]\n" + "\n\n---\n".join(parts)


def cached_prefix(few_shot_count: int = 6) -> str:
    version = active_version()
    body_path = PROMPTS_DIR / "system" / f"{version}_body.txt"
    if body_path.exists():
        system_text = body_path.read_text()
    else:
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
        "poc": fp.poc_price,
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
            "wave": wave_ctx,
            "sweep": sweep_ctx,
            "big_trade": big_trade_ctx,
        },
        "vp_setup": _vp_setup_ctx(symbol, primary, cached_daily, htf, session_cvd),
    }
    return json.dumps(payload)


def _sym_abbrev(symbol: str) -> str:
    """XAUTUSDT → XAU, BTCUSDT → BTC, etc."""
    s = symbol.upper().replace("USDT", "").replace("USD", "")
    return s[:6]


def build_cached_prefix(few_shot_count: int = 6) -> str:
    """Build the cacheable prefix: v4 system prompt + tool schema + few-shots.

    This block is marked cache_control by llm/client.py and paid once per
    5-min Anthropic cache window (~4000-5000 tokens, zero per-call after hit).
    """
    body_path = PROMPTS_DIR / "system" / "v4_body.txt"
    if body_path.exists():
        system_text = body_path.read_text()
    else:
        version = active_version()
        system_text = (PROMPTS_DIR / "system" / f"{version}.txt").read_text()

    # Append tool schema
    try:
        from llm.schema import GRID_PLAN_TOOL
        tool_block = "\n\n[TOOL SCHEMA]\n" + json.dumps(GRID_PLAN_TOOL, indent=2)
    except Exception:
        tool_block = ""

    return system_text + tool_block + _few_shot_block(few_shot_count)


def build_compact_suffix(
    primary: list[Bar],
    direction_result: dict | None = None,
    atr_val: float | None = None,
) -> str:
    """Build the compact variable suffix (~140 tokens for FULL mode).

    Format:
        sym=XAU p=4456 atr=8 reg=trend_up:.72 shape=b htf=bull
        vp:poc=4455 va=4448-4462 hvn=4450,4458
        dir=long conf=.85 sig=continuation
        z=+0:.92,-4:.85,-8:.78
        d=-45,-120,30,-200,-125
        [anchors_inrange=...]   ← only when price within 0.3×ATR of anchor
        [sweeps_active=...]     ← only when fresh sweep with delta_confirms
    """
    if not primary:
        return ""

    symbol = primary[0].symbol
    bar = primary[-1]
    current_price = bar.ohlc.c

    # ATR
    if atr_val is None or atr_val <= 0:
        try:
            from pipeline.features.atr import atr_from_store
            atr_val = atr_from_store(symbol, primary[0].tf, period=14)
        except Exception:
            atr_val = 0.0
    if atr_val <= 0:
        atr_val = max(bar.ohlc.h - bar.ohlc.l, 1e-6)

    # Regime
    reg_str = "uncertain:.50"
    shape_str = "D"
    try:
        from pipeline.features.day_type import classify as _dt_classify
        from pipeline.state_store import store as _store
        session_bars = _store().recent(symbol, primary[0].tf, 500)
        from types import SimpleNamespace as _NS
        import pipeline.features.vp_cache as _vpc
        _cached_daily = _vpc.get(symbol, "daily")
        _daily_vp_ns = _NS(**_cached_daily) if _cached_daily else None
        _dt = _dt_classify(session_bars or primary, daily_vp=_daily_vp_ns)
        reg_str = f"{_dt.type}:{_dt.confidence:.2f}"
        if _cached_daily:
            shape_str = _cached_daily.get("shape", "D") or "D"
    except Exception:
        pass

    # HTF bias
    htf_str = "neutral"
    try:
        from pipeline.features.session import htf_bias as _htf_bias
        from pipeline.state_store import store as _store
        daily_bars = _store().recent(symbol, "1d", 30) or []
        if daily_bars:
            htf_str = _htf_bias(daily_bars).get("bias", "neutral")
    except Exception:
        pass

    # VP block
    poc_str = vah_str = val_str = ""
    hvn_str = ""
    try:
        import pipeline.features.vp_cache as _vpc
        _vp = _vpc.get(symbol, "daily")
        if _vp:
            poc_str = str(round(float(_vp.get("poc", 0) or 0), 2))
            vah_str = str(round(float(_vp.get("vah", 0) or 0), 2))
            val_str = str(round(float(_vp.get("val", 0) or 0), 2))
            _hvns = (_vp.get("hvn_zones") or [])[:2]
            hvn_prices = [round((h["low"] + h["high"]) / 2, 2) for h in _hvns]
            hvn_str = ",".join(str(p) for p in hvn_prices)
    except Exception:
        pass

    # Direction
    dir_str = conf_str = sig_str = "flat"
    conf_str = ".50"
    sig_str = "none"
    if direction_result:
        dir_str = direction_result.get("side", "flat")
        _conf = direction_result.get("confidence", 0.5)
        conf_str = f"{_conf:.2f}".lstrip("0") or ".50"
        sig_str = direction_result.get("signal", direction_result.get("sig", "none"))
    else:
        dir_str = "flat"

    # Entry zones as offsets
    z_parts: list[str] = []
    try:
        from execution.zone_collector import collect as _collect_zones
        _side = dir_str if dir_str != "flat" else "long"
        _zones = _collect_zones(symbol, _side, current_price, n=5, min_gap=atr_val * 0.3)
        for z in _zones[:3]:
            offset = round(z.price - current_price, 2)
            sign = "+" if offset >= 0 else ""
            z_parts.append(f"{sign}{offset}:{z.strength:.2f}".rstrip("0").rstrip("."))
    except Exception:
        pass
    z_str = ",".join(z_parts) if z_parts else "+0:.80"

    # Last 5 bar deltas
    d_parts: list[str] = []
    try:
        fps = [build_fp(b) for b in primary[-5:]]
        from pipeline.features import bar_delta as _bar_delta
        d_parts = [str(round(_bar_delta(fp))) for fp in reversed(fps)]
    except Exception:
        pass
    d_str = ",".join(d_parts) if d_parts else "0"

    abbrev = _sym_abbrev(symbol)
    lines = [
        f"sym={abbrev} p={round(current_price,2)} atr={round(atr_val,2)} reg={reg_str} shape={shape_str} htf={htf_str}",
    ]

    vp_line_parts = []
    if poc_str:
        vp_line_parts.append(f"poc={poc_str}")
    if val_str and vah_str:
        vp_line_parts.append(f"va={val_str}-{vah_str}")
    if hvn_str:
        vp_line_parts.append(f"hvn={hvn_str}")
    if vp_line_parts:
        lines.append("vp:" + " ".join(vp_line_parts))

    lines.append(f"dir={dir_str} conf={conf_str} sig={sig_str}")
    lines.append(f"z={z_str}")
    lines.append(f"d={d_str}")

    # Optional: active anchor bars in range
    try:
        from pipeline.features.anchor_bar import active_anchors as _active_anchors
        anchors = _active_anchors(symbol, current_price, atr_val)
        if anchors:
            parts = [f"{round((a.high+a.low)/2,2)}:{a.delta_sign} age={a.age_bars} vol_z={a.vol_z:.1f}"
                     for a in anchors[:3]]
            lines.append(f"anchors_inrange=[{', '.join(parts)}]")
    except Exception:
        pass

    # Optional: active sweeps
    try:
        from pipeline.features.sweep import active_sweeps as _active_sweeps
        sweeps = _active_sweeps(symbol)
        fresh = [s for s in sweeps if s.delta_confirms and s.age_bars <= 10]
        if fresh:
            parts = [f"{s.swept_level}:{s.sweep_type.replace('sweep_','')} age={s.age_bars}"
                     for s in fresh[:2]]
            lines.append(f"sweeps_active=[{', '.join(parts)}]")
    except Exception:
        pass

    return "\n".join(lines)


def _coarse_footprint(bars: list[Bar], bin_size: float, top_k: int = 10) -> str:
    """Re-bin footprint into wider price buckets for coarse_footprint mode.

    Returns multi-line string, one line per bar:
      b-2: <price>[<delta>:<vol>] ...
      b-1: ...
      b0:  ...
    """
    if not bars:
        return ""
    lines = []
    n = len(bars)
    for i, bar in enumerate(bars):
        label = f"b{i - n + 1}" if i < n - 1 else "b0"
        label = label.replace("b-0", "b0")
        fp = build_fp(bar)
        # Aggregate cells into wider bins: delta = ask - bid, vol = ask + bid
        bins: dict[float, list] = {}  # bin_low → [delta, vol]
        for cell in fp.cells:
            bin_low = (cell.price // bin_size) * bin_size
            b = bins.setdefault(bin_low, [0.0, 0.0])
            b[0] += cell.ask_vol - cell.bid_vol
            b[1] += cell.ask_vol + cell.bid_vol
        # Sort by vol descending, keep top_k
        top = sorted(bins.items(), key=lambda x: x[1][1], reverse=True)[:top_k]
        top.sort(key=lambda x: x[0])  # re-sort by price for readability
        level_strs = [f"{round(bl,2)}[{round(dv[0]):+d}:{round(dv[1])}]"
                      for bl, dv in top if dv[1] > 0]
        lines.append(f" {label}: {' '.join(level_strs)}")
    return "\n".join(lines)


def build_coarse_footprint_suffix(
    primary: list[Bar],
    direction_result: dict | None = None,
    atr_val: float | None = None,
    bin_size: float | None = None,
    full_fp_bars: int = 3,
    delta_tail: int = 5,
    top_k: int = 10,
) -> str:
    """~250-token variant: compact header + coarse-binned footprint for last 3 bars
    + delta-only tail for bars -3 to -7.
    """
    if not primary:
        return ""

    symbol = primary[0].symbol

    if bin_size is None:
        try:
            import pipeline.features.vp_cache as _vpc
            _cfg = (_vpc._load() or {}).get(symbol, {})
            _inst = _cfg.get("instrument") or {}
            bin_size = float(_inst.get("coarse_fp_bin_size") or 2.5)
        except Exception:
            bin_size = 2.5

    # Build compact header (same as compact suffix but without z= and d=)
    compact = build_compact_suffix(primary, direction_result, atr_val)
    compact_lines = compact.splitlines()
    # Keep header lines, replace d= with coarse footprint block
    header_lines = [l for l in compact_lines if not l.startswith("d=")]

    # Full footprint: last full_fp_bars bars
    fp_bars = primary[-full_fp_bars:] if len(primary) >= full_fp_bars else primary
    fp_block = _coarse_footprint(fp_bars, bin_size, top_k)

    # Delta tail: bars before the fp_bars
    tail_bars = primary[-(full_fp_bars + delta_tail):-full_fp_bars] if len(primary) > full_fp_bars else []
    d_tail_parts: list[str] = []
    if tail_bars:
        try:
            fps = [build_fp(b) for b in tail_bars]
            from pipeline.features import bar_delta as _bar_delta
            d_tail_parts = [str(round(_bar_delta(fp))) for fp in reversed(fps)]
        except Exception:
            pass
    d_tail_line = f"d_tail={','.join(d_tail_parts)}" if d_tail_parts else ""

    parts = header_lines
    if fp_block:
        parts += [f"fp bins={bin_size}"] + fp_block.splitlines()
    if d_tail_line:
        parts.append(d_tail_line)

    return "\n".join(parts)


def _vp_setup_ctx(symbol: str, primary: list, cached_daily, htf: dict, session_cvd: float) -> dict | None:
    """Compute VP setup + confluence scores for the current bar."""
    try:
        from pipeline.features.vp_setup import from_store as _vps
        from pipeline.features.confluence import score_zones as _score, grid_tier_spacing as _tiers
        from pipeline.features.vp_cache import poc_sequence as _pocs, get_history as _hist
        from pipeline.footprint import build as _bfp

        setup = _vps(symbol, primary[0].tf if primary else "1m")
        if setup.setup_type == "none":
            return None

        bar = primary[-1] if primary else None
        fp = _bfp(bar) if bar else None
        htf_bias = htf.get("bias", "neutral") if htf else "neutral"

        zone_scores: list[dict] = []
        if bar and fp and setup.entry_zones:
            for sc in _score(setup.entry_zones, bar, fp, cached_daily, setup.bias, session_cvd, htf_bias):
                zone_scores.append({
                    "price":    sc.price,
                    "score":    sc.score,
                    "qty_pct":  sc.qty_pct,
                    "skip":     sc.skip,
                    "reasons":  sc.reasons,
                })

        tiers = _tiers(cached_daily)

        return {
            "setup_type":    setup.setup_type,
            "bias":          setup.bias,
            "entry_zones":   setup.entry_zones,
            "target_zones":  setup.target_zones,
            "max_legs":      setup.max_legs,
            "setup_strength": setup.setup_strength,
            "grid_mode":     setup.grid_mode,
            "reason":        setup.reason,
            "zone_scores":   zone_scores,
            "tier_widths":   tiers,
        }
    except Exception:
        return None
