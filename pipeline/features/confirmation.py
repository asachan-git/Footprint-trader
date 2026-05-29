"""Orderflow Confirmation Taxonomy — three weighted-score patterns.

Three patterns drive entry/exit decisions. Each condition contributes a weight;
pattern fires when total weight ≥ 0.6. Confidence = total weight (capped 1.0).
All multi-bar conditions use a 20-bar lookback.

ABSORPTION  (mean-reversion / defended level):
  Canonical: delta at bar extreme is OPPOSITE to bar direction.
  Bear candle + buy-vol at high = buyers tried, bears won → fade up, SHORT signal.
  Bull candle + sell-vol at low = sellers tried, bulls won → fade down, LONG signal.

EXHAUSTION  (counter-trend / trend running out):
  Extension ≥ N ATR from daily POC + CVD divergence + big_trade exhausted signal.
  Default threshold: 3.0 ATR (tunable per symbol via config/settings.yaml).

CONTINUATION (trend-follow / momentum confirmed):
  CVD breakout/reclaim in direction + big_trade pushed at LVN + structural alignment.

Threshold: 0.60 for all three patterns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CONFIRMATION_THRESHOLD = 0.60
DEFAULT_EXHAUSTION_ATR_THRESHOLD = 3.0


@dataclass
class ConditionResult:
    name: str
    weight: float
    passed: bool
    detail: str = ""


@dataclass
class PatternResult:
    pattern: str              # "absorption" | "exhaustion" | "continuation"
    side: str                 # "long" | "short" | "neutral"
    weight_total: float       # sum of passed condition weights
    fired: bool               # weight_total >= CONFIRMATION_THRESHOLD
    confidence: float         # = min(1.0, weight_total) when fired, else weight_total
    conditions: list[ConditionResult] = field(default_factory=list)
    reason: str = ""


def _no_pattern(pattern: str) -> PatternResult:
    return PatternResult(pattern=pattern, side="neutral", weight_total=0.0,
                         fired=False, confidence=0.0)


# ── ABSORPTION ────────────────────────────────────────────────────────────────

def detect_absorption(
    bars: list,
    daily_vp: dict | None = None,
    atr_val: float | None = None,
    swing_levels: list[float] | None = None,
    active_sweeps: list | None = None,
) -> PatternResult:
    """Canonical absorption on the latest bar + structural context.

    Returns PatternResult with side = the FADE direction
    (bear candle absorbing buyers → short signal).
    """
    if not bars:
        return _no_pattern("absorption")

    bar = bars[-1]
    recent = bars[-20:] if len(bars) >= 20 else bars

    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    total_range = h - l
    if total_range <= 0:
        return _no_pattern("absorption")

    extreme_zone = total_range * 0.10
    is_bear = c < o
    is_bull = c > o

    conditions: list[ConditionResult] = []
    absorbed_side: str = "neutral"  # aggressor side that got absorbed
    fade_side: str = "neutral"      # the trade direction (opposite of absorbed)

    # ── Condition 1: delta at extreme OPPOSITE to bar direction (0.30) ──
    canon_weight = 0.0
    canon_detail = ""
    if is_bear and bar.bid_ladder and bar.ask_ladder:
        upper_threshold = h - extreme_zone
        bid_upper = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price >= upper_threshold)
        ask_upper = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price >= upper_threshold)
        if ask_upper > 0 and bid_upper / ask_upper >= 1.5:
            canon_weight = 0.30
            canon_detail = f"bear candle: bid_upper={bid_upper:.0f} ≥ 1.5× ask_upper={ask_upper:.0f}"
            absorbed_side = "buy"
            fade_side = "short"
    elif is_bull and bar.bid_ladder and bar.ask_ladder:
        lower_threshold = l + extreme_zone
        ask_lower = sum(lvl.vol for lvl in bar.ask_ladder if lvl.price <= lower_threshold)
        bid_lower = sum(lvl.vol for lvl in bar.bid_ladder if lvl.price <= lower_threshold)
        if bid_lower > 0 and ask_lower / bid_lower >= 1.5:
            canon_weight = 0.30
            canon_detail = f"bull candle: ask_lower={ask_lower:.0f} ≥ 1.5× bid_lower={bid_lower:.0f}"
            absorbed_side = "sell"
            fade_side = "long"
    conditions.append(ConditionResult(
        "canonical_delta", canon_weight, canon_weight > 0, canon_detail
    ))

    if fade_side == "neutral":
        # No canonical absorption detected — don't score remaining conditions
        return PatternResult(pattern="absorption", side="neutral", weight_total=0.0,
                             fired=False, confidence=0.0, conditions=conditions,
                             reason="no canonical absorption")

    # ── Condition 2: bar range compressed vs 20-bar avg (0.15) ──
    avg_range = sum(abs(b.ohlc.h - b.ohlc.l) for b in recent) / len(recent) if recent else total_range
    range_compressed = total_range < avg_range * 0.7
    conditions.append(ConditionResult(
        "range_compressed", 0.15 if range_compressed else 0.0, range_compressed,
        f"range={total_range:.2f} avg={avg_range:.2f} ratio={total_range/avg_range:.2f}"
    ))

    # ── Condition 3: at structural level (0.20) ──
    at_level = False
    level_detail = ""
    if atr_val and atr_val > 0:
        margin = atr_val * 0.3
        vp_levels: list[float] = []
        if daily_vp:
            for k in ("vah", "val", "poc"):
                v = daily_vp.get(k)
                if v:
                    vp_levels.append(v)
            for hvn in daily_vp.get("hvn_zones", []):
                mid = (hvn.get("low", 0) + hvn.get("high", 0)) / 2
                if mid:
                    vp_levels.append(mid)
        if swing_levels:
            vp_levels.extend(swing_levels)
        if active_sweeps:
            for ev in active_sweeps:
                vp_levels.append(ev.swept_level)
        for lvl in vp_levels:
            if abs(c - lvl) <= margin:
                at_level = True
                level_detail = f"within 0.3 ATR of level {lvl:.2f}"
                break
    conditions.append(ConditionResult(
        "at_structural_level", 0.20 if at_level else 0.0, at_level, level_detail
    ))

    # ── Condition 4: big_trade = absorbed at level (0.20) ──
    big_trade_absorbed = False
    try:
        from pipeline.features.big_trade import get_recent_events
        events = get_recent_events(bar.symbol, bar.tf, n=20)
        big_trade_absorbed = any(
            e.outcome == "absorbed" and
            (abs(e.price - c) <= (atr_val or 1.0) * 0.5)
            for e in events
        )
    except Exception:
        pass
    conditions.append(ConditionResult(
        "big_trade_absorbed", 0.20 if big_trade_absorbed else 0.0, big_trade_absorbed,
        "big_trade=absorbed near level" if big_trade_absorbed else ""
    ))

    # ── Condition 5: stacked imbalance against absorbed aggression (0.15) ──
    stacked_confirms = False
    try:
        from pipeline.footprint import build as build_fp
        from pipeline.features.stacked_imbalance import stacked_imbalances
        fp = build_fp(bar)
        zones = stacked_imbalances(fp, min_stack=3)
        for z in zones:
            # absorbed_side="buy" (buyers at high) → stacked SELL imbalance at high confirms
            # absorbed_side="sell" (sellers at low) → stacked BUY imbalance at low confirms
            if absorbed_side == "buy" and z.side == "sell" and z.price_high >= h - extreme_zone:
                stacked_confirms = True
                break
            if absorbed_side == "sell" and z.side == "buy" and z.price_low <= l + extreme_zone:
                stacked_confirms = True
                break
    except Exception:
        pass
    conditions.append(ConditionResult(
        "stacked_imbalance_confirms", 0.15 if stacked_confirms else 0.0, stacked_confirms,
        f"stacked {absorbed_side} imb at extreme" if stacked_confirms else ""
    ))

    total = sum(c.weight for c in conditions)
    fired = total >= CONFIRMATION_THRESHOLD
    return PatternResult(
        pattern="absorption",
        side=fade_side,
        weight_total=round(total, 3),
        fired=fired,
        confidence=round(min(1.0, total), 3),
        conditions=conditions,
        reason=f"absorbed {absorbed_side}s at {h if absorbed_side == 'buy' else l:.2f}, fade={fade_side}",
    )


# ── EXHAUSTION ────────────────────────────────────────────────────────────────

def detect_exhaustion(
    bars: list,
    daily_vp: dict | None = None,
    atr_val: float | None = None,
    exhaustion_atr_threshold: float = DEFAULT_EXHAUSTION_ATR_THRESHOLD,
) -> PatternResult:
    """Extension from POC + CVD divergence + big_trade exhausted.

    Returns PatternResult with side = direction of COUNTER-TREND entry.
    """
    if not bars or not atr_val or atr_val <= 0:
        return _no_pattern("exhaustion")

    bar = bars[-1]
    recent = bars[-20:] if len(bars) >= 20 else bars
    conditions: list[ConditionResult] = []

    poc = daily_vp.get("poc") if daily_vp else None
    vah = daily_vp.get("vah") if daily_vp else None
    val = daily_vp.get("val") if daily_vp else None
    current_price = bar.ohlc.c

    # ── Condition 1: extension ≥ N ATR from daily POC (0.25) ──
    extension_ok = False
    extension_atrs = 0.0
    if poc:
        extension_atrs = abs(current_price - poc) / atr_val
        extension_ok = extension_atrs >= exhaustion_atr_threshold
    conditions.append(ConditionResult(
        "extension_from_poc", 0.25 if extension_ok else 0.0, extension_ok,
        f"extension={extension_atrs:.1f} ATR from POC={poc}"
    ))

    # Determine counter-trend side from extension direction
    if poc:
        counter_side = "short" if current_price > poc else "long"
    else:
        counter_side = "neutral"

    # ── Condition 2: CVD candle signal — divergence/hidden/wick_trap (0.25) ──
    cvd_signal_ok = False
    cvd_detail = ""
    try:
        from pipeline.features.cvd_candlestick import detect as cvd_detect
        sig = cvd_detect(recent)
        divergence_patterns = {
            "divergence_bear", "divergence_bull",
            "hidden_buying", "hidden_selling",
            "wick_trap",
        }
        if sig.pattern in divergence_patterns:
            cvd_signal_ok = True
            cvd_detail = f"cvd:{sig.pattern} conf={sig.confidence:.2f}"
    except Exception:
        pass
    conditions.append(ConditionResult(
        "cvd_reversal_signal", 0.25 if cvd_signal_ok else 0.0, cvd_signal_ok, cvd_detail
    ))

    # ── Condition 3: opposite-side absorption in last 20 bars (0.20) ──
    opp_absorption = False
    try:
        from pipeline.features.absorption import detect_canonical_absorption
        from pipeline.footprint import build as build_fp
        for b in recent[-5:]:
            fp = build_fp(b)
            abs_results = detect_canonical_absorption(b, fp)
            for a in abs_results:
                # counter_side=short needs bullish absorption at current level (sellers absorbed)
                # counter_side=long needs bearish absorption at current level (buyers absorbed)
                if counter_side == "short" and a.side == "buy":
                    opp_absorption = True; break
                if counter_side == "long" and a.side == "sell":
                    opp_absorption = True; break
            if opp_absorption:
                break
    except Exception:
        pass
    conditions.append(ConditionResult(
        "opposite_absorption", 0.20 if opp_absorption else 0.0, opp_absorption,
        "opposite absorption in last 5 bars" if opp_absorption else ""
    ))

    # ── Condition 4: big_trade = exhausted (0.20) ──
    big_exhausted = False
    try:
        from pipeline.features.big_trade import get_recent_events
        events = get_recent_events(bar.symbol, bar.tf, n=20)
        big_exhausted = any(e.outcome == "exhausted" for e in events)
    except Exception:
        pass
    conditions.append(ConditionResult(
        "big_trade_exhausted", 0.20 if big_exhausted else 0.0, big_exhausted,
        "big_trade=exhausted in session" if big_exhausted else ""
    ))

    # ── Condition 5: distance to VAH/VAL < 0.5 ATR (0.10) ──
    near_va_boundary = False
    if vah and val:
        dist_vah = abs(current_price - vah) / atr_val
        dist_val = abs(current_price - val) / atr_val
        near_va_boundary = min(dist_vah, dist_val) < 0.5
    conditions.append(ConditionResult(
        "near_va_boundary", 0.10 if near_va_boundary else 0.0, near_va_boundary,
        f"dist_to_va={min(abs(current_price-(vah or 0)), abs(current_price-(val or 0))):.2f}" if (vah and val) else ""
    ))

    total = sum(c.weight for c in conditions)
    fired = total >= CONFIRMATION_THRESHOLD
    return PatternResult(
        pattern="exhaustion",
        side=counter_side if fired else "neutral",
        weight_total=round(total, 3),
        fired=fired,
        confidence=round(min(1.0, total), 3),
        conditions=conditions,
        reason=(f"extension={extension_atrs:.1f}×ATR from POC={poc:.2f}, "
                f"counter={counter_side}") if poc else "no_poc",
    )


# ── CONTINUATION ──────────────────────────────────────────────────────────────

def detect_continuation(
    bars: list,
    side: str,                         # "long" | "short" — the intended trade direction
    daily_vp: dict | None = None,
    atr_val: float | None = None,
) -> PatternResult:
    """Trend-follow confirmation for the given side.

    Returns PatternResult with side = same as input side when fired.
    """
    if not bars or side not in ("long", "short"):
        return _no_pattern("continuation")

    bar = bars[-1]
    recent = bars[-20:] if len(bars) >= 20 else bars
    conditions: list[ConditionResult] = []
    symbol = getattr(bar, "symbol", "")
    tf = getattr(bar, "tf", "15m")

    # ── Condition 1: stacked imbalances same direction (0.10) ──
    stacked_direction = False
    try:
        from pipeline.footprint import build as build_fp
        from pipeline.features.stacked_imbalance import stacked_imbalances
        target_imb_side = "buy" if side == "long" else "sell"
        stacked_count = 0
        for b in recent[-5:]:
            fp = build_fp(b)
            zones = stacked_imbalances(fp, min_stack=3)
            stacked_count += sum(1 for z in zones if z.side == target_imb_side)
        stacked_direction = stacked_count >= 2
    except Exception:
        pass
    conditions.append(ConditionResult(
        "stacked_imbalance", 0.10 if stacked_direction else 0.0, stacked_direction,
        f"stacked {side} imbalances in recent bars" if stacked_direction else ""
    ))

    # ── Condition 2: CVD breakout/reclaim in trade direction (0.25) ──
    cvd_momentum = False
    cvd_detail = ""
    try:
        from pipeline.features.cvd_candlestick import detect as cvd_detect
        sig = cvd_detect(recent)
        long_patterns = {"breakout_long", "reclaim", "hidden_buying"}
        short_patterns = {"breakout_short", "wick_trap", "hidden_selling"}
        if side == "long" and sig.pattern in long_patterns:
            cvd_momentum = True
            cvd_detail = f"cvd:{sig.pattern} conf={sig.confidence:.2f}"
        elif side == "short" and sig.pattern in short_patterns:
            cvd_momentum = True
            cvd_detail = f"cvd:{sig.pattern} conf={sig.confidence:.2f}"
    except Exception:
        pass
    conditions.append(ConditionResult(
        "cvd_momentum", 0.25 if cvd_momentum else 0.0, cvd_momentum, cvd_detail
    ))

    # ── Condition 3: FVG in trend direction (0.15) ──
    fvg_aligns = False
    try:
        from pipeline.features.fvg import detect_fvgs, annotate_all
        fvgs = detect_fvgs(recent, max_age_bars=20)
        hvn = (daily_vp or {}).get("hvn_zones", [])
        lvn = (daily_vp or {}).get("lvn_zones", [])
        afvgs = annotate_all(fvgs, hvn, lvn)
        current_price = bar.ohlc.c
        for afvg in afvgs:
            if side == "long" and afvg.side == "bull":
                fvg_aligns = True; break
            if side == "short" and afvg.side == "bear":
                fvg_aligns = True; break
    except Exception:
        pass
    conditions.append(ConditionResult(
        "fvg_direction", 0.15 if fvg_aligns else 0.0, fvg_aligns,
        f"unfilled {side} FVG present" if fvg_aligns else ""
    ))

    # ── Condition 4: big_trade = pushed at LVN (0.20) ──
    big_pushed_lvn = False
    try:
        from pipeline.features.big_trade import get_recent_events
        events = get_recent_events(symbol, tf, n=20)
        aggressor = "buy" if side == "long" else "sell"
        big_pushed_lvn = any(
            e.outcome == "pushed" and e.vp_context == "at_lvn" and e.aggressor == aggressor
            for e in events
        )
    except Exception:
        pass
    conditions.append(ConditionResult(
        "big_trade_pushed_lvn", 0.20 if big_pushed_lvn else 0.0, big_pushed_lvn,
        "big_trade=pushed at LVN" if big_pushed_lvn else ""
    ))

    # ── Condition 5: day_type matches direction (0.10) ──
    daytype_matches = False
    try:
        from pipeline.features.day_type import get_regime
        regime = get_regime(symbol, tf)
        if side == "long" and regime.type == "trend_up":
            daytype_matches = True
        elif side == "short" and regime.type == "trend_down":
            daytype_matches = True
    except Exception:
        pass
    conditions.append(ConditionResult(
        "daytype_match", 0.10 if daytype_matches else 0.0, daytype_matches,
        f"regime matches {side}" if daytype_matches else ""
    ))

    # ── Condition 6: price at anchor bar level + same delta sign (0.20) ──
    anchor_defended = False
    try:
        from pipeline.features.anchor_bar import active_anchors, test_retest
        cur_price = bar.ohlc.c
        atr_use = atr_val or 1.0
        anchors = active_anchors(symbol, cur_price, atr_use)
        for a in anchors:
            r = test_retest(a, bar)
            if r.pattern == "continuation":
                if side == "long" and a.delta_sign == "bull":
                    anchor_defended = True; break
                if side == "short" and a.delta_sign == "bear":
                    anchor_defended = True; break
    except Exception:
        pass
    conditions.append(ConditionResult(
        "anchor_defended", 0.20 if anchor_defended else 0.0, anchor_defended,
        f"anchor bar level defended for {side}" if anchor_defended else ""
    ))

    total = sum(c.weight for c in conditions)
    fired = total >= CONFIRMATION_THRESHOLD
    return PatternResult(
        pattern="continuation",
        side=side if fired else "neutral",
        weight_total=round(total, 3),
        fired=fired,
        confidence=round(min(1.0, total), 3),
        conditions=conditions,
        reason=f"continuation {side} weight={total:.2f}",
    )


# ── Convenience: run all three ────────────────────────────────────────────────

def run_all(
    bars: list,
    candidate_side: str = "long",
    daily_vp: dict | None = None,
    atr_val: float | None = None,
    swing_levels: list[float] | None = None,
    active_sweeps: list | None = None,
    exhaustion_atr_threshold: float = DEFAULT_EXHAUSTION_ATR_THRESHOLD,
) -> tuple[PatternResult, PatternResult, PatternResult]:
    """Run absorption, exhaustion, continuation for the given candidate_side.

    Returns (absorption, exhaustion, continuation).
    """
    absorption = detect_absorption(
        bars, daily_vp=daily_vp, atr_val=atr_val,
        swing_levels=swing_levels, active_sweeps=active_sweeps,
    )
    exhaustion = detect_exhaustion(
        bars, daily_vp=daily_vp, atr_val=atr_val,
        exhaustion_atr_threshold=exhaustion_atr_threshold,
    )
    continuation = detect_continuation(
        bars, side=candidate_side, daily_vp=daily_vp, atr_val=atr_val,
    )
    return absorption, exhaustion, continuation


def from_store(symbol: str, tf: str = "15m") -> tuple[PatternResult, PatternResult]:
    """Convenience: run absorption + exhaustion from state_store.

    Used by direction_engine and decision_validator.
    Returns (absorption, exhaustion) — continuation requires explicit side.
    """
    try:
        from pipeline.state_store import store
        from pipeline.features.atr import atr
        from pipeline.features.vp_cache import get as vp_get
        from pipeline.features.sweep import active_sweeps as get_sweeps
        from pipeline.features.swing import get as get_swing

        bars = store().recent(symbol, tf, 25)
        if not bars:
            return _no_pattern("absorption"), _no_pattern("exhaustion")

        atr_val = atr(bars)
        daily_vp = vp_get(symbol, "daily")
        sweeps = get_sweeps(symbol)
        sp = get_swing(symbol)
        swing_levels = []
        if sp:
            from pipeline.features.swing import all_reference_levels
            swing_levels = [lvl for _, lvl in all_reference_levels(sp)]

        absorption = detect_absorption(
            bars, daily_vp=daily_vp, atr_val=atr_val,
            swing_levels=swing_levels, active_sweeps=sweeps,
        )
        exhaustion = detect_exhaustion(
            bars, daily_vp=daily_vp, atr_val=atr_val,
        )
        return absorption, exhaustion
    except Exception as e:
        log.debug(f"[confirmation.from_store] {e}")
        return _no_pattern("absorption"), _no_pattern("exhaustion")
