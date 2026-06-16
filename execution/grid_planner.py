"""Grid planner — turns footprint/orderflow context into a concrete neutral-grid
placement plan that the MQL5 EA places verbatim via WebRequest.

The grid is a NEUTRAL position-placement model: legs are straddled around a
decision-point fulcrum so a strong move EITHER way pays. The edge is WHERE the
fulcrum is and HOW the grid is sized — not direction prediction. An optional
*skew* loads the favoured side (scale into the winner) without abandoning the
neutral straddle.

Pipeline (plan_grid_levels):
  1. regime        — day_type.get_regime (range vs trend, max_legs)
  2. triggers      — zone_triggers.detect_all
  3. _score_and_pick — confluence-scored best fulcrum   [NEW: the edge]
  4. _should_skip  — chop gate (inside HVN / at POC / uncertain+flat) [NEW]
  5. _size_grid    — N + step from ATR + swing-range + candle [NEW]
  6. _resolve_skew — which side to load                 [NEW]
  7. _build_legs   — fulcrum ± i·step prices + lot ladder [NEW]
  8. _resolve_tps  — snap TP to real structure (REUSE zone_collector)

Reused: day_type, zone_triggers, zone_collector, atr_from_store, grid_modes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from execution import zone_triggers
from execution.zone_triggers import Trigger

# step / lot-ladder constants live with the existing mechanical placer
from execution.grid_modes import (
    MEAN_REV_STEP_MULT,
    TREND_STEP_MULT,
)


@dataclass
class Leg:
    price: float
    lot: float


@dataclass
class GridPlan:
    verdict: str = "skip"             # "arm" | "skip"
    skip_reason: str = ""
    fulcrum: float = 0.0
    regime: str = "unknown"
    regime_confidence: float = 0.0
    trigger_kind: str = ""
    trigger_confidence: float = 0.0
    n_per_side: int = 0
    step: float = 0.0
    skew: str = "none"                # "buy" | "sell" | "none"
    skew_reason: str = ""
    buy_legs: list[Leg] = field(default_factory=list)
    sell_legs: list[Leg] = field(default_factory=list)
    buy_tp: float = 0.0
    sell_tp: float = 0.0
    atr: float = 0.0
    swing_range: float = 0.0
    plan_id: str = ""


# ── confluence scoring (the edge) ───────────────────────────────────────────

def _bb_stretch(bars, current_price: float, period: int = 20, mult: float = 2.0) -> float:
    """Bollinger stretch in [0,1]: how far price sits beyond the ±mult·stdev band
    around the SMA. 0 = inside band, →1 = far outside. No repo helper exists, so
    computed inline from closes."""
    closes = [b.ohlc.c for b in bars][-period:]
    if len(closes) < period:
        return 0.0
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    band = mult * sd
    excess = abs(current_price - mean) - band
    if excess <= 0:
        return 0.0
    return min(1.0, excess / band)


def _score_and_pick(triggers: list[Trigger], regime, current_price: float,
                    daily_vp: dict | None, bars) -> Trigger | None:
    """Pick the highest-confluence trigger. Score = intrinsic confidence +
    bonuses for (a) BB-stretch confluence, (b) agreement with an independent
    structural zone, (c) regime-consistency."""
    if not triggers:
        return None

    stretch = _bb_stretch(bars, current_price)

    # Independent structural levels for agreement bonus.
    zone_prices: list[float] = []
    try:
        from execution.zone_collector import _all_zones
        zone_prices = [z.price for z in _all_zones(daily_vp.get("_symbol") if daily_vp else "")]
    except Exception:
        zone_prices = []

    rtype = getattr(regime, "type", "uncertain")

    best, best_score = None, -1.0
    for t in triggers:
        score = t.confidence

        # (a) BB-stretch: a stretched-price fulcrum is a better reaction point.
        score += 0.15 * stretch

        # (b) agreement: another source within ~0.1% of the fulcrum.
        tol = max(current_price * 0.001, 1e-9)
        if any(abs(zp - t.fulcrum_price) <= tol for zp in zone_prices):
            score += 0.15

        # (c) regime-consistency: trend trigger in trend, range trigger in range.
        interp = t.context.get("interpretation", "")
        if rtype == "range" and interp in ("reclaim", "reversal"):
            score += 0.10
        if rtype in ("trend_up", "trend_down") and interp in ("break_sustain", "continuation"):
            score += 0.10

        if score > best_score:
            best, best_score = t, score
    return best


# ── chop gate ───────────────────────────────────────────────────────────────

def _price_inside_hvn(price: float, daily_vp: dict | None) -> bool:
    if not daily_vp:
        return False
    for hvn in daily_vp.get("hvn_zones") or []:
        if float(hvn["low"]) <= price <= float(hvn["high"]):
            return True
    return False


def _should_skip(trigger: Trigger | None, regime, fulcrum: float,
                 daily_vp: dict | None) -> tuple[bool, str]:
    """Don't arm a straddle where price will oscillate through both ladders."""
    if trigger is None:
        return True, "no_trigger"
    # HVN-edge / inside-touch triggers ARE meant to sit at a node edge; only skip
    # non-HVN fulcrums that fall *inside* a node body.
    if trigger.kind not in ("hvn_edge", "hvn_inside_touch") and _price_inside_hvn(fulcrum, daily_vp):
        return True, "chop:inside_hvn"
    if daily_vp and daily_vp.get("current_position") == "at_poc":
        return True, "chop:at_poc"
    # Genuine balance/chop = regime is uncertain AND there IS initial-balance data
    # showing little expansion. When regime is simply absent (no session data, e.g.
    # historical replay), don't hard-skip — the trigger itself carries the edge.
    rtype = getattr(regime, "type", "uncertain")
    ib_range = getattr(regime, "ib_range", 0.0) or 0.0
    if (rtype == "uncertain" and ib_range > 0
            and getattr(regime, "ib_expansion_pct", 0.0) < 0.5):
        return True, "chop:uncertain_no_expansion"
    return False, ""


# ── sizing ──────────────────────────────────────────────────────────────────

def _opposing_swing_range(symbol: str, fulcrum: float, skew_dir: str,
                          daily_vp: dict | None, atr: float) -> float:
    """Distance from fulcrum to the nearest strong opposing structure — the
    grid's reach. Uses zone_collector; falls back to ~3·ATR or VA width."""
    try:
        from execution.zone_collector import collect
        # look both ways; take nearest non-trivial structure either side
        for direction in ("long", "short"):
            zones = collect(symbol, direction, fulcrum, n=1)
            if zones:
                d = abs(zones[0].price - fulcrum)
                if d > 1e-9:
                    return d
    except Exception:
        pass
    if daily_vp and daily_vp.get("va_width"):
        return float(daily_vp["va_width"])
    return max(atr * 3.0, 1e-6)


def _candle_conviction(symbol: str, tf: str) -> float:
    """0..1 from the latest bar's volume & directional delta — bigger conviction
    earns more legs."""
    try:
        from pipeline.state_store import store
        bars = store().recent(symbol, tf, 60)
        if len(bars) < 2:
            return 0.5
        cur = bars[-1]
        vol = sum(l.vol for l in cur.bid_ladder) + sum(l.vol for l in cur.ask_ladder)
        avg = sum(
            sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)
            for b in bars[:-1]
        ) / max(1, len(bars) - 1)
        vol_z = (vol / avg) if avg > 0 else 1.0
        directional = abs(cur.delta or 0.0) / vol if vol > 0 else 0.0
        return max(0.0, min(1.0, 0.3 * min(2.0, vol_z) + 0.7 * directional))
    except Exception:
        return 0.5


def _size_grid(trigger: Trigger, regime, atr: float, swing_range: float,
               conviction: float, hvn_max_legs: int = 8) -> tuple[int, float]:
    """N (capped by regime.max_legs) and step ($) from ATR + swing range +
    candle conviction. HVN node width floors the step so legs span the node."""
    rtype = getattr(regime, "type", "uncertain")
    step_mult = TREND_STEP_MULT if rtype in ("trend_up", "trend_down") else MEAN_REV_STEP_MULT
    max_legs = int(getattr(regime, "max_legs", 5) or 5)

    # hvn_inside_touch: spacing is pure ATR-mult; leg count = node_width / step so a
    # WIDER node gets MORE legs (the user's rule). No width-floor on the step (that
    # would widen spacing on big nodes → fewer legs, the opposite of intended). Uses
    # its OWN cap (hvn_max_legs), not the regime chop cap — the skip-gate already
    # filters genuine chop, and width-driven count is the whole point here.
    if trigger.kind == "hvn_inside_touch":
        step = (step_mult * atr) if atr > 0 else max(trigger.raw_range / 8.0, 1e-6)
        n = int(round(trigger.raw_range / step)) if step > 0 else 2
        n = max(2, min(hvn_max_legs, n))
        return n, round(step, 4)

    step = step_mult * atr if atr > 0 else max(trigger.raw_range / 4.0, 1e-6)

    # HVN node width: ensure legs straddle the whole node, not a sliver of it.
    if trigger.raw_range > 0:
        step = max(step, trigger.raw_range / 4.0)
    if step <= 0:
        step = max(swing_range / 5.0, 1e-6)

    legs_to_cover = int(swing_range / step) if step > 0 else max_legs
    n = max(2, min(max_legs, legs_to_cover))
    # conviction scales N upward toward the cap.
    n = max(2, min(max_legs, int(round(n * (0.7 + 0.6 * conviction)))))
    return n, round(step, 4)


# ── skew ────────────────────────────────────────────────────────────────────

def _resolve_skew(trigger: Trigger, regime) -> tuple[str, str]:
    """Which side to load (scale into the winner). Neutral straddle remains;
    skew only weights one side."""
    bias = trigger.context.get("bias", "none")
    interp = trigger.context.get("interpretation", "")
    rtype = getattr(regime, "type", "uncertain")

    if bias in ("buy", "sell"):
        why = f"{trigger.kind}:{interp or 'bias'} → load {bias}"
        return bias, why
    if rtype == "trend_up":
        return "buy", "trend_up regime → load buy"
    if rtype == "trend_down":
        return "sell", "trend_down regime → load sell"
    return "none", "no directional lean → symmetric"


# ── legs ────────────────────────────────────────────────────────────────────

def _ladder(n: int, base_lot: float, lot_step: float, heavy_near_mid: bool) -> list[float]:
    """LINEAR_REVERSED (heavy near mid) or LINEAR (light near mid), matching the
    EA's ResolveLotForIndex semantics. Index 1 = nearest the fulcrum."""
    lots = []
    for i in range(1, n + 1):
        if heavy_near_mid:
            raw = base_lot + (n - i) * lot_step
        else:
            raw = base_lot + (i - 1) * lot_step
        lots.append(round(max(base_lot, raw), 2))
    return lots


def _build_legs(fulcrum: float, n: int, step: float, skew: str,
                base_lot: float, lot_step: float) -> tuple[list[Leg], list[Leg]]:
    """fulcrum ± i·step prices. Favoured side gets the heavier/longer ladder."""
    buy_n = n
    sell_n = n
    # Skew adds one extra leg + heavier ladder to the favoured side.
    if skew == "buy":
        buy_n = n + 1
    elif skew == "sell":
        sell_n = n + 1

    buy_lots = _ladder(buy_n, base_lot, lot_step, heavy_near_mid=(skew == "buy"))
    sell_lots = _ladder(sell_n, base_lot, lot_step, heavy_near_mid=(skew == "sell"))

    buy_legs = [Leg(price=round(fulcrum + i * step, 4), lot=buy_lots[i - 1])
                for i in range(1, buy_n + 1)]
    sell_legs = [Leg(price=round(fulcrum - i * step, 4), lot=sell_lots[i - 1])
                 for i in range(1, sell_n + 1)]
    return buy_legs, sell_legs


# ── TP snapping ─────────────────────────────────────────────────────────────

def _resolve_tps(symbol: str, fulcrum: float, buy_legs: list[Leg],
                 sell_legs: list[Leg], atr: float, tp_mult: float = 1.5) -> tuple[float, float]:
    """Snap TP beyond the outer leg to the nearest strong structural zone;
    fallback outer_leg ± tp_mult·ATR."""
    top = max((l.price for l in buy_legs), default=fulcrum)
    bot = min((l.price for l in sell_legs), default=fulcrum)
    buy_tp = top + tp_mult * atr
    sell_tp = bot - tp_mult * atr
    try:
        from execution.zone_collector import _all_zones
        zones = [z for z in _all_zones(symbol) if z.strength >= 0.6]
        above = [z.price for z in zones if z.price > top]
        below = [z.price for z in zones if z.price < bot]
        if above:
            buy_tp = min(above)
        if below:
            sell_tp = max(below)
    except Exception:
        pass
    return round(buy_tp, 4), round(sell_tp, 4)


# ── public entry ────────────────────────────────────────────────────────────

def plan_grid_levels(symbol: str, tf: str, current_price: float,
                     trigger_hint: str = "", settings: dict | None = None) -> GridPlan:
    """Compute a neutral-grid plan for `symbol`/`tf` at `current_price`.
    Returns GridPlan(verdict="arm"|"skip")."""
    settings = settings or {}
    grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
    base_lot = float(grid_cfg.get("base_lot", 0.01))
    lot_step = float(grid_cfg.get("lot_step", 0.01))
    tp_mult = float(grid_cfg.get("tp_atr_mult", 1.5))
    hvn_max_legs = int(grid_cfg.get("hvn_max_legs", 8))

    from pipeline.state_store import store
    from pipeline.features.atr import atr_from_store
    from pipeline.features import day_type
    try:
        from pipeline.features.vp_cache import get as vp_get
        daily_vp = vp_get(symbol, "daily")
        if daily_vp is not None:
            daily_vp = dict(daily_vp)
            daily_vp["_symbol"] = symbol  # let downstream helpers recover symbol
    except Exception:
        daily_vp = None

    bars = store().recent(symbol, tf, 60)
    atr = 0.0
    try:
        atr = atr_from_store(symbol, tf) or 0.0
    except Exception:
        atr = 0.0

    try:
        regime = day_type.get_regime(symbol, "1m")
    except Exception:
        regime = None

    plan_id = f"{symbol}-{tf}-{(bars[-1].close_ts if bars else 0)}"

    triggers = zone_triggers.detect_all(symbol, tf, current_price, regime,
                                        atr=atr, daily_vp=daily_vp)
    if trigger_hint:
        hinted = [t for t in triggers if t.kind == trigger_hint]
        if hinted:
            triggers = hinted

    fulcrum_t = _score_and_pick(triggers, regime, current_price, daily_vp, bars)

    skip, reason = _should_skip(fulcrum_t, regime,
                                fulcrum_t.fulcrum_price if fulcrum_t else current_price,
                                daily_vp)
    if skip:
        return GridPlan(verdict="skip", skip_reason=reason,
                        regime=getattr(regime, "type", "unknown"),
                        atr=round(atr, 4), plan_id=plan_id)

    fulcrum = fulcrum_t.fulcrum_price
    skew, skew_reason = _resolve_skew(fulcrum_t, regime)
    swing_range = _opposing_swing_range(symbol, fulcrum, skew, daily_vp, atr)
    conviction = _candle_conviction(symbol, tf)
    n, step = _size_grid(fulcrum_t, regime, atr, swing_range, conviction,
                         hvn_max_legs=hvn_max_legs)
    buy_legs, sell_legs = _build_legs(fulcrum, n, step, skew, base_lot, lot_step)
    buy_tp, sell_tp = _resolve_tps(symbol, fulcrum, buy_legs, sell_legs, atr, tp_mult)

    return GridPlan(
        verdict="arm",
        fulcrum=round(fulcrum, 4),
        regime=getattr(regime, "type", "unknown"),
        regime_confidence=round(float(getattr(regime, "confidence", 0.0) or 0.0), 3),
        trigger_kind=fulcrum_t.kind,
        trigger_confidence=round(fulcrum_t.confidence, 3),
        n_per_side=n,
        step=step,
        skew=skew,
        skew_reason=skew_reason,
        buy_legs=buy_legs,
        sell_legs=sell_legs,
        buy_tp=buy_tp,
        sell_tp=sell_tp,
        atr=round(atr, 4),
        swing_range=round(swing_range, 4),
        plan_id=plan_id,
    )
