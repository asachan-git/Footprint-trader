"""TP Participation Scorer — per-bar participation score for open positions.

Computes a score s ∈ [-1, +1] measuring how aggressively the market is
participating in the direction of the open trade. Used to proactively
extend or shrink TP.

Score components:
  +0.25  cvd_slope_20bar       (normalized by ATR, in trade direction)
  +0.15  absorption_count      (same side, last 20 bars, normalized 0..1)
  +0.15  stacked_imb           (same side, last 20 bars, normalized 0..1)
  +0.15  big_trade_pushed      (in direction, last 20 bars, normalized 0..1)
  -0.20  opposite_absorption_near_tp (within 0.3 ATR, last 20 bars)
  -0.10  big_trade_absorbed    (against direction, near TP, last 20 bars)

Actions per score:
  s ≥ +0.5   → extend TP to next zone toward direction (skip nearest, jump one)
  -0.3..+0.5 → hold TP
  s < -0.3   → shrink TP to nearer zone between avg_entry and current TP
  opposite_absorption AT current TP → immediate shrink regardless of s

Hysteresis:
  TP can shift at most once per 3 bars (lock_bars=3).
  Direction (extend/shrink) cannot reverse within 5 bars (direction_lock=5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCORE_EXTEND_THRESHOLD = 0.50
SCORE_SHRINK_THRESHOLD = -0.30
LOCK_BARS = 3
DIRECTION_LOCK_BARS = 5
NEAR_TP_ATR_FRACTION = 0.30


@dataclass
class ParticipationScore:
    score: float             # -1.0 .. +1.0
    action: str              # "extend" | "shrink" | "hold"
    components: dict         # breakdown of score
    reason: str
    immediate_shrink: bool = False  # True when opposite absorption AT current TP


def compute(
    side: str,              # "long" | "short" — trade direction
    bars: list,             # last 20 bars
    current_tp: float,
    avg_entry: float,
    atr_val: float,
    daily_vp: dict | None = None,
) -> ParticipationScore:
    """Compute participation score for current position.

    Args:
        side:       Trade direction ("long" | "short").
        bars:       Recent bars in the position's TF (last 20).
        current_tp: Current take-profit price.
        avg_entry:  Average fill price of position.
        atr_val:    Current ATR value.
        daily_vp:   Daily VP dict (for zone target selection).
    """
    if not bars or side not in ("long", "short") or atr_val <= 0:
        return ParticipationScore(0.0, "hold", {}, "insufficient data")

    recent = bars[-20:] if len(bars) >= 20 else bars
    components: dict = {}

    # ── +0.25: CVD slope in trade direction ────────────────────────────────────
    cvd_score = 0.0
    try:
        deltas = [b.delta or 0 for b in recent]
        n = len(deltas)
        if n >= 5:
            # Slope = last 5 bars CVD change vs prior 15 bars
            recent_cvd = sum(deltas[-5:])
            prior_cvd = sum(deltas[:-5]) / max(n - 5, 1)
            slope = recent_cvd / (atr_val * n + 1e-9)
            if side == "long":
                cvd_score = max(-0.25, min(0.25, slope * 0.25))
            else:
                cvd_score = max(-0.25, min(0.25, -slope * 0.25))
    except Exception:
        pass
    components["cvd_slope"] = round(cvd_score, 3)

    # ── +0.15: absorption count same side ─────────────────────────────────────
    absorption_score = 0.0
    try:
        from pipeline.features.absorption import detect_canonical_absorption
        from pipeline.footprint import build as build_fp
        target_absorp_side = "buy" if side == "long" else "sell"
        count = 0
        for b in recent:
            fp = build_fp(b)
            for a in detect_canonical_absorption(b, fp):
                if a.side == target_absorp_side:
                    count += 1
        absorption_score = min(0.15, count / max(len(recent), 1) * 0.15 * 5)
    except Exception:
        pass
    components["absorption_same_side"] = round(absorption_score, 3)

    # ── +0.15: stacked imbalance same direction ────────────────────────────────
    stacked_score = 0.0
    try:
        from pipeline.footprint import build as build_fp
        from pipeline.features.stacked_imbalance import stacked_imbalances
        target_imb_side = "buy" if side == "long" else "sell"
        zone_count = 0
        for b in recent[-5:]:
            fp = build_fp(b)
            zones = stacked_imbalances(fp, min_stack=3)
            zone_count += sum(1 for z in zones if z.side == target_imb_side)
        stacked_score = min(0.15, zone_count / 5 * 0.15)
    except Exception:
        pass
    components["stacked_imbalance"] = round(stacked_score, 3)

    # ── +0.15: big_trade pushed in direction ───────────────────────────────────
    pushed_score = 0.0
    try:
        from pipeline.features.big_trade import get_recent_events
        bar = recent[-1]
        events = get_recent_events(bar.symbol, bar.tf, n=20)
        aggressor = "buy" if side == "long" else "sell"
        pushed = sum(1 for e in events if e.outcome == "pushed" and e.aggressor == aggressor)
        pushed_score = min(0.15, pushed / 5 * 0.15)
    except Exception:
        pass
    components["big_trade_pushed"] = round(pushed_score, 3)

    # ── -0.20: opposite absorption near TP ────────────────────────────────────
    opp_absorption_neg = 0.0
    immediate_shrink = False
    try:
        from pipeline.features.absorption import detect_canonical_absorption
        from pipeline.footprint import build as build_fp
        opp_side = "sell" if side == "long" else "buy"
        near_tp_margin = atr_val * NEAR_TP_ATR_FRACTION
        near_tp_count = 0
        at_tp_count = 0
        for b in recent:
            fp = build_fp(b)
            for a in detect_canonical_absorption(b, fp):
                if a.side == opp_side and abs(a.price - current_tp) <= near_tp_margin:
                    near_tp_count += 1
                    if abs(a.price - current_tp) <= atr_val * 0.1:
                        at_tp_count += 1
        opp_absorption_neg = min(0.20, near_tp_count / max(len(recent), 1) * 0.20 * 5)
        immediate_shrink = at_tp_count >= 2
    except Exception:
        pass
    components["opp_absorption_near_tp"] = round(-opp_absorption_neg, 3)

    # ── -0.10: big_trade absorbed against direction near TP ───────────────────
    absorbed_neg = 0.0
    try:
        from pipeline.features.big_trade import get_recent_events
        bar = recent[-1]
        events = get_recent_events(bar.symbol, bar.tf, n=20)
        opp_aggressor = "sell" if side == "long" else "buy"
        near_tp_absorbed = sum(
            1 for e in events
            if e.outcome == "absorbed" and e.aggressor == opp_aggressor
            and abs(e.price - current_tp) <= atr_val * NEAR_TP_ATR_FRACTION
        )
        absorbed_neg = min(0.10, near_tp_absorbed / 3 * 0.10)
    except Exception:
        pass
    components["big_trade_absorbed_near_tp"] = round(-absorbed_neg, 3)

    # ── Final score ─────────────────────────────────────────────────────────────
    raw_score = (
        cvd_score + absorption_score + stacked_score + pushed_score
        - opp_absorption_neg - absorbed_neg
    )
    score = round(max(-1.0, min(1.0, raw_score)), 3)

    if immediate_shrink:
        action = "shrink"
    elif score >= SCORE_EXTEND_THRESHOLD:
        action = "extend"
    elif score < SCORE_SHRINK_THRESHOLD:
        action = "shrink"
    else:
        action = "hold"

    reason = f"s={score:.2f} cvd={cvd_score:.2f} abs={absorption_score:.2f} stk={stacked_score:.2f}"
    return ParticipationScore(
        score=score, action=action, components=components,
        reason=reason, immediate_shrink=immediate_shrink,
    )


# ── Per-bar TP adjustment ──────────────────────────────────────────────────────

@dataclass
class _HysteresisState:
    bars_since_last_shift: int = LOCK_BARS  # start at floor so first bar is immediately eligible
    last_action: str = "hold"
    bars_since_direction_change: int = DIRECTION_LOCK_BARS


_hysteresis: dict[str, _HysteresisState] = {}


def adjust_tp(
    position_id: str,
    side: str,
    bars: list,
    current_tp: float,
    avg_entry: float,
    atr_val: float,
    daily_vp: dict | None = None,
) -> tuple[float, str]:
    """Compute new TP based on participation score. Respects hysteresis.

    Returns (new_tp, reason). new_tp == current_tp means no change.
    """
    state = _hysteresis.setdefault(position_id, _HysteresisState())
    state.bars_since_last_shift += 1
    state.bars_since_direction_change += 1

    ps = compute(side, bars, current_tp, avg_entry, atr_val, daily_vp)

    if ps.immediate_shrink:
        # Bypass hysteresis — immediate shrink
        new_tp = _shrink_tp(side, current_tp, avg_entry, atr_val, daily_vp)
        state.bars_since_last_shift = 0
        state.last_action = "shrink"
        return new_tp, f"immediate_shrink opp_absorption_at_tp"

    if state.bars_since_last_shift < LOCK_BARS:
        return current_tp, f"locked ({state.bars_since_last_shift}/{LOCK_BARS})"

    if ps.action == "hold":
        return current_tp, ps.reason

    # Direction lock — can't reverse within DIRECTION_LOCK_BARS
    if (ps.action != state.last_action and state.last_action != "hold"
            and state.bars_since_direction_change < DIRECTION_LOCK_BARS):
        return current_tp, f"direction_locked ({state.bars_since_direction_change}/{DIRECTION_LOCK_BARS})"

    if ps.action == "extend":
        new_tp = _extend_tp(side, current_tp, atr_val, daily_vp)
        changed = new_tp != current_tp
    else:
        new_tp = _shrink_tp(side, current_tp, avg_entry, atr_val, daily_vp)
        changed = new_tp != current_tp

    if changed:
        if ps.action != state.last_action and state.last_action != "hold":
            state.bars_since_direction_change = 0
        state.last_action = ps.action
        state.bars_since_last_shift = 0

    return new_tp, ps.reason


def _extend_tp(side: str, current_tp: float, atr_val: float, daily_vp: dict | None) -> float:
    """Move TP one zone further in trade direction."""
    poc = (daily_vp or {}).get("poc")
    vah = (daily_vp or {}).get("vah")
    val = (daily_vp or {}).get("val")

    if side == "long":
        candidates = [p for p in (poc, vah) if p and p > current_tp]
        if candidates:
            return min(candidates)
        return round(current_tp + atr_val * 0.5, 4)
    else:
        candidates = [p for p in (poc, val) if p and p < current_tp]
        if candidates:
            return max(candidates)
        return round(current_tp - atr_val * 0.5, 4)


def _shrink_tp(side: str, current_tp: float, avg_entry: float, atr_val: float, daily_vp: dict | None) -> float:
    """Move TP one zone closer to avg_entry."""
    poc = (daily_vp or {}).get("poc")
    vah = (daily_vp or {}).get("vah")
    val = (daily_vp or {}).get("val")

    min_profit_dist = atr_val * 0.3  # don't shrink below 0.3 ATR profit

    if side == "long":
        floor_tp = avg_entry + min_profit_dist
        candidates = [p for p in (poc, val) if p and floor_tp < p < current_tp]
        if candidates:
            return max(candidates)
        return max(floor_tp, round(current_tp - atr_val * 0.5, 4))
    else:
        ceil_tp = avg_entry - min_profit_dist
        candidates = [p for p in (poc, vah) if p and current_tp < p < ceil_tp]
        if candidates:
            return min(candidates)
        return min(ceil_tp, round(current_tp + atr_val * 0.5, 4))


def cleanup(position_id: str) -> None:
    """Remove hysteresis state when position closes."""
    _hysteresis.pop(position_id, None)
