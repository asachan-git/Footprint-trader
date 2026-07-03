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
from dataclasses import dataclass, field, replace

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
    buy_sl: float = 0.0   # 0 = no SL (default); set for displacement trigger
    sell_sl: float = 0.0
    atr: float = 0.0
    swing_range: float = 0.0
    plan_id: str = ""
    # Venue rebasing: the plan is computed in the ANALYSIS frame (Binance/Bybit)
    # then re-anchored on the EXECUTION venue's live price before it reaches the
    # broker. analysis_anchor / venue_anchor record both ends; rebased flags it.
    analysis_anchor: float = 0.0
    venue_anchor: float = 0.0
    rebased: bool = False
    trigger_context: dict = field(default_factory=dict)   # detector metadata (edge side, session…)
    # Squeeze A/B label — ALWAYS computed (vol compressed/coiled at arm?), independent of
    # whether require_squeeze_gate enforces it. Carried through the cycle so the exit audit
    # can bucket outcomes squeeze-pass vs squeeze-fail.
    squeeze_ok: bool = False
    squeeze_rank: float = 1.0
    base_lot: float = 0.0   # ladder base — used by enqueue to re-index lots for behind-market skips
    lot_step: float = 0.0


# trigger-hint groups: a hint may name one kind, a comma list, or a group keyword.
# vp_level_touch intentionally EXCLUDED — disabled per user (HVN-inside-touch + squeeze
# only). The detector still runs but can never win the structural hint, so no VP-level
# straddles arm. Re-add "vp_level_touch" here to revive it.
_HINT_GROUPS = {
    "structural": {"hvn_inside_touch", "squeeze", "hvn_displacement", "bb_expansion_touch"},
    # LVN displacement: vp_level_touch is the only detector that arms on an LVN.
    # The planner narrows it to level_type=="lvn" (see plan_grid_levels) so only the
    # vacuum fires. Neutral straddle-in-vacuum: price sits in the LVN, leaves fast one
    # side, that leg fills + runs to the bounding HVN; the opposite leg never triggers.
    "lvn_displacement": {"vp_level_touch"},
    # VP-level setup (parallel setup #3): VAL/VAH reclaim-or-break-sustain (va) + POC /
    # VAH / VAL support-resistance touches (vp_level_touch). Each arms its own magic
    # (va=strat 7, vp=strat 3) so it runs parallel to hvn (1) and squeeze (2).
    "vp_levels": {"va", "vp_level_touch"},
}


def _hint_set(hint: str) -> set[str]:
    """Resolve a trigger_hint into the set of acceptable trigger kinds.
    "" → empty (no filter); a group keyword → its members; else comma-split kinds."""
    if not hint:
        return set()
    if hint in _HINT_GROUPS:
        return set(_HINT_GROUPS[hint])
    return {k.strip() for k in hint.split(",") if k.strip()}


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
    # HVN-edge / inside-touch / VP-level triggers ARE meant to sit ON structure
    # (an HVN edge or a POC/VA/LVN line, which usually lives inside a node); only skip
    # other fulcrums that fall *inside* a node body.
    if (trigger.kind not in ("hvn_edge", "hvn_inside_touch", "hvn_displacement",
                             "vp_level_touch", "squeeze", "bb_expansion_touch")
            and _price_inside_hvn(fulcrum, daily_vp)):
        return True, "chop:inside_hvn"
    if daily_vp and daily_vp.get("current_position") == "at_poc":
        # a POC / naked-POC fulcrum SHOULD arm at the POC — that's the setup, not chop.
        # hvn_inside_touch is ALSO exempt: an edge-rejection tap is a directional signal
        # (price rejected the HVN boundary), not POC balance — straddle the tapped edge.
        _poc_ok = (trigger.kind == "vp_level_touch"
                   and trigger.context.get("level_type") in ("poc", "naked_poc"))
        _structural = trigger.kind in ("hvn_inside_touch", "hvn_displacement",
                                       "bb_expansion_touch")
        if not (_poc_ok or _structural):
            return True, "chop:at_poc"
    # Genuine balance/chop = regime is uncertain AND there IS initial-balance data
    # showing little expansion. Structural HVN moves (displacement/touch) are exempt —
    # they represent committed directional flow between zones, not oscillation.
    rtype = getattr(regime, "type", "uncertain")
    ib_range = getattr(regime, "ib_range", 0.0) or 0.0
    _is_structural = trigger.kind in ("hvn_inside_touch", "hvn_displacement",
                                      "bb_expansion_touch")
    if (not _is_structural and rtype == "uncertain" and ib_range > 0
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
               conviction: float, hvn_max_legs: int = 8,
               lvn_legs_per_side: int = 1,
               mean_rev_step_mult: float = MEAN_REV_STEP_MULT) -> tuple[int, float]:
    """N (capped by regime.max_legs) and step ($) from ATR + swing range +
    candle conviction. HVN node width floors the step so legs span the node."""
    rtype = getattr(regime, "type", "uncertain")
    step_mult = TREND_STEP_MULT if rtype in ("trend_up", "trend_down") else mean_rev_step_mult
    max_legs = int(getattr(regime, "max_legs", 5) or 5)

    # LVN displacement (straddle-in-vacuum): step = node_width/2 puts the inner buy/
    # sell legs EXACTLY on the LVN edges. Price exiting the vacuum trips the edge leg
    # immediately and rides to the bounding HVN (the trigger's tp_up/tp_down). FEW
    # legs — displacement is a fast one-way thrust, not an oscillation to scale into.
    # Independent of regime.max_legs / ATR: the vacuum width is the whole geometry.
    if trigger.kind == "vp_level_touch" and trigger.context.get("level_type") == "lvn":
        node_width = float(trigger.context.get("node_width", 0.0) or 0.0)
        if node_width <= 0:
            node_width = float(trigger.raw_range or 0.0)
        n = max(1, int(lvn_legs_per_side))
        step = (node_width / 2.0) if node_width > 0 else max(
            (step_mult * atr) if atr > 0 else swing_range / 5.0, 1e-6)
        return n, round(step, 4)

    # hvn_edge: n fixed by TF (hvn_max_legs_by_tf), step = ATR × mult.
    # Step is TF-ATR-driven so spacing adapts to market volatility on the trigger TF.
    if trigger.kind == "hvn_edge":
        n = max(2, hvn_max_legs)
        step = max((step_mult * atr) if atr > 0 else 1.0, 1e-4)
        return n, round(step, 4)

    # hvn_inside_touch: WIDTH-DRIVEN sizing (BTC Jun22 regime). step = ATR × mult; leg
    # count = node_width / step so a WIDER node gets MORE legs (legs span the whole node),
    # capped at hvn_max_legs (per-TF). raw_range = node width for this trigger.
    if trigger.kind == "hvn_inside_touch":
        step = (step_mult * atr) if atr > 0 else max(trigger.raw_range / 8.0, 1e-6)
        n = int(round(trigger.raw_range / step)) if step > 0 else 2
        n = max(2, min(hvn_max_legs, n))
        return n, round(step, 4)

    # candle_sweep / engulf: same zone-width logic as hvn_inside_touch.
    # n = hvn_max_legs (per TF); step = candle_hl / n so the ladder spans one candle
    # range per side. buy legs start above candle_high; sell below candle_low.
    if trigger.kind == "candle_sweep":
        candle_hl = float(trigger.raw_range or 0.0)
        n = max(2, hvn_max_legs)
        step = round(candle_hl / n, 4) if (candle_hl > 0 and n > 0) \
               else max((step_mult * atr) if atr > 0 else 1.0, 1e-4)
        return n, round(step, 4)

    # bb_expansion_touch: ATR-based spacing, leg count from zone width / step.
    if trigger.kind == "bb_expansion_touch":
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

def _resolve_skew(trigger: Trigger, regime, cfg: dict | None = None,
                  symbol: str = "", tf: str = "") -> tuple[str, str]:
    """Which side to load (scale into the winner) — a VOTE SUM across signals. Net sign
    picks the side; net zero stays symmetric. Directional inventory (skew) also makes a
    net-profit exit price exist (L_buy ≠ L_sell). Votes (buy +, sell −):
      • HVN inside-touch reversion (weight 2 — the core thesis): tapped top → fade down
        (sell), bottom → fade up (buy). Strong enough that one counter-vote can't flip it.
      • detector context bias / regime trend / HTF-20MA bias / 2.5σ edge (weight 1 each).
    """
    cfg = cfg or {}
    votes = 0
    why: list[str] = []

    if trigger.kind == "hvn_inside_touch" and bool(cfg.get("hvn_reversion_bias", True)):
        edge = trigger.context.get("edge", "")
        if edge == "top":
            votes -= 2; why.append("hvn reject-top→fade down")
        elif edge == "bottom":
            votes += 2; why.append("hvn reject-bottom→fade up")

    # bb_expansion_touch: footprint-confirmed absorption → weight 2 (strong directional);
    # no absorption (continuation/neutral) → weight 1 (HTF-aligned fade only).
    if trigger.kind == "bb_expansion_touch":
        fp_signal = trigger.context.get("fp_signal", "neutral")
        weight = 2 if fp_signal == "absorption" else 1
        b = trigger.context.get("bias", "none")
        if b == "sell":
            votes -= weight; why.append(f"bb_expansion {fp_signal}→sell")
        elif b == "buy":
            votes += weight; why.append(f"bb_expansion {fp_signal}→buy")

    bias = trigger.context.get("bias", "none")
    if bias == "buy":
        votes += 1; why.append(f"{trigger.kind} bias buy")
    elif bias == "sell":
        votes -= 1; why.append(f"{trigger.kind} bias sell")

    rtype = getattr(regime, "type", "uncertain")
    if rtype == "trend_up":
        votes += 1; why.append("regime up")
    elif rtype == "trend_down":
        votes -= 1; why.append("regime down")

    if symbol and tf:
        hv, hw = zone_triggers.bb_htf_bias(symbol, tf, cfg)
        if hv:
            votes += hv; why.append(hw)
        ev, ew = zone_triggers.bb_edge_vote(symbol, tf, cfg)
        if ev:
            votes += ev; why.append(ew)

    side = "buy" if votes > 0 else "sell" if votes < 0 else "none"
    return side, (f"skew={side}(Σ{votes:+d}): " + "; ".join(why) if why else "symmetric")


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

    # Both sides increase with distance from fulcrum (light near mid, heavy far).
    # The bias side still gets the extra leg and wider total exposure via skew;
    # accumulating more size as price moves deeper is better than front-loading the first fill.
    buy_lots = _ladder(buy_n, base_lot, lot_step, heavy_near_mid=False)
    sell_lots = _ladder(sell_n, base_lot, lot_step, heavy_near_mid=False)

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


# ── venue rebasing ───────────────────────────────────────────────────────────

def _rebase_to_venue(plan: GridPlan, analysis_anchor: float, venue_price: float) -> GridPlan:
    """Re-anchor a plan computed in the analysis frame (Binance/Bybit) onto the
    execution venue's live price (Vantage). Every absolute level — fulcrum, each
    leg, both TPs, the step — is scaled by the ratio venue/analysis, preserving the
    structural % geometry while moving it to where the broker actually quotes. Same
    principle as execution.venue_translator, applied to the neutral grid.

    Why this is mandatory: a BuyStop must sit above the venue ask and a SellStop
    below the venue bid. Binance-frame absolute prices won't satisfy that on Vantage
    → MT5 rejects the orders. (Tick-rounding + broker min-stop-distance are enforced
    EA-side, since only the terminal knows the symbol's stopsLevel.)
    """
    if analysis_anchor <= 0 or venue_price <= 0:
        return plan
    ratio = venue_price / analysis_anchor
    if abs(ratio - 1.0) < 1e-9:
        # in-frame caller (dashboard/sim) — identity, just stamp the anchors
        return replace(plan, analysis_anchor=round(analysis_anchor, 4),
                       venue_anchor=round(venue_price, 4), rebased=False)
    return replace(
        plan,
        fulcrum=round(plan.fulcrum * ratio, 4),
        step=round(plan.step * ratio, 4),
        buy_legs=[Leg(price=round(l.price * ratio, 4), lot=l.lot) for l in plan.buy_legs],
        sell_legs=[Leg(price=round(l.price * ratio, 4), lot=l.lot) for l in plan.sell_legs],
        buy_tp=round(plan.buy_tp * ratio, 4),
        sell_tp=round(plan.sell_tp * ratio, 4),
        buy_sl=round(plan.buy_sl * ratio, 4) if plan.buy_sl else 0.0,
        sell_sl=round(plan.sell_sl * ratio, 4) if plan.sell_sl else 0.0,
        analysis_anchor=round(analysis_anchor, 4),
        venue_anchor=round(venue_price, 4),
        rebased=True,
    )


# ── public entry ────────────────────────────────────────────────────────────

def plan_grid_levels(symbol: str, tf: str, current_price: float,
                     trigger_hint: str = "", settings: dict | None = None,
                     venue_price: float | None = None,
                     min_step_venue: float = 0.0,
                     force_trigger: "Trigger | None" = None) -> GridPlan:
    """Compute a neutral-grid plan for `symbol`/`tf`.

    `current_price` is the ANALYSIS-frame price (Binance/Bybit) used for structure.
    `venue_price`, when given, is the EXECUTION-venue (Vantage) live price the EA
    sent: the finished plan is rebased onto it so the legs land on the correct side
    of the broker's market. Omitted → no rebase (in-frame callers).
    Returns GridPlan(verdict="arm"|"skip")."""
    settings = settings or {}
    grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
    base_lot = float(grid_cfg.get("base_lot", 0.01))
    lot_step = float(grid_cfg.get("lot_step", 0.01))
    tp_mult = float(grid_cfg.get("tp_atr_mult", 1.5))
    # Per-TF leg cap: a fast TF should run a tighter ladder than a slow one. The cycle's
    # TF is known here, so hvn_max_legs_by_tf (keyed "1m"/"5m"/"15m"/"1h") overrides the
    # global hvn_max_legs. Any TF absent falls back to the global.
    hvn_max_legs = int(grid_cfg.get("hvn_max_legs", 8))
    mean_rev_step_mult = float(grid_cfg.get("mean_rev_step_mult") or MEAN_REV_STEP_MULT)
    _legs_by_tf = grid_cfg.get("hvn_max_legs_by_tf") or {}
    if isinstance(_legs_by_tf, dict) and tf and tf in _legs_by_tf:
        hvn_max_legs = int(_legs_by_tf.get(tf) or hvn_max_legs)
    _mult_by_tf = grid_cfg.get("mean_rev_step_mult_by_tf") or {}
    if isinstance(_mult_by_tf, dict) and tf and tf in _mult_by_tf:
        mean_rev_step_mult = float(_mult_by_tf[tf] or mean_rev_step_mult)
    lvn_legs_per_side = int(grid_cfg.get("lvn_legs_per_side", 1))
    max_fulcrum_dist_pct = float(grid_cfg.get("max_fulcrum_dist_pct", 0.05))

    from pipeline.state_store import store
    from pipeline.features.atr import atr_from_store
    from pipeline.features import day_type
    try:
        from pipeline.features.vp_cache import get_prev_and_today
        _prev_vp, _today_vp = get_prev_and_today(symbol)
        # Merge prev-D and today's zones so both are trigger candidates at all times.
        # POC/VAH/VAL: prefer today's if available (forming session reference), else prev-D.
        # HVN/LVN zone lists: union of both periods (deduped by proximity handled downstream).
        if _prev_vp or _today_vp:
            _base = dict(_prev_vp or _today_vp)
            if _today_vp and _prev_vp:
                _merged_hvn = list(_prev_vp.get("hvn_zones") or []) + list(_today_vp.get("hvn_zones") or [])
                _merged_lvn = list(_prev_vp.get("lvn_zones") or []) + list(_today_vp.get("lvn_zones") or [])
                _base["hvn_zones"] = _merged_hvn
                _base["lvn_zones"] = _merged_lvn
                # today's POC/VAH/VAL only if session has enough data (poc > 0)
                if _today_vp.get("poc"):
                    _base["poc"] = _today_vp["poc"]
                    _base["vah"] = _today_vp.get("vah") or _base.get("vah")
                    _base["val"] = _today_vp.get("val") or _base.get("val")
            daily_vp = _base
            daily_vp["_symbol"] = symbol
        else:
            daily_vp = None
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

    _no_trig_reason = ""
    if force_trigger is not None:
        # Intrabar touch-arm: caller already resolved the tapped edge as the Trigger —
        # skip detection/scoring, use it verbatim (still subject to the skip gates below).
        fulcrum_t = force_trigger
    else:
        all_triggers = zone_triggers.detect_all(symbol, tf, current_price, regime,
                                               atr=atr, daily_vp=daily_vp, cfg=grid_cfg)
        hint_set = _hint_set(trigger_hint)
        if hint_set:
            hinted = [t for t in all_triggers if t.kind in hint_set]
            triggers = hinted  # empty → no trigger → fulcrum_t=None → clean skip, not wrong-kind win
        else:
            triggers = all_triggers
        # lvn_displacement narrows vp_level_touch to the VACUUM only (LVN), excluding
        # POC/VA/naked-POC taps — those are mean-revert lines, not displacement vacuums.
        if trigger_hint == "lvn_displacement":
            lvn_only = [t for t in triggers if t.context.get("level_type") == "lvn"]
            triggers = lvn_only

        fulcrum_t = _score_and_pick(triggers, regime, current_price, daily_vp, bars)

        # Build a descriptive no-trigger reason for the emit log.
        # kept_kinds = detectors that matched the hint (relevant); other_kinds = rest (context only).
        if fulcrum_t is None:
            kept_kinds  = [t.kind for t in triggers]
            other_kinds = [t.kind for t in all_triggers if t.kind not in {t2.kind for t2 in triggers}]
            if not all_triggers:
                _no_trig_reason = f"no_{trigger_hint}"
            elif not kept_kinds:
                # hint-specific detectors didn't fire; show other fires as context
                others_str = f",others={','.join(other_kinds)}" if other_kinds else ""
                _no_trig_reason = f"no_{trigger_hint}{others_str}"
            else:
                # hint detectors fired but got gated downstream (chop/poc/squeeze/etc.)
                _no_trig_reason = f"no_{trigger_hint}:gated({','.join(kept_kinds)})"
        else:
            _no_trig_reason = ""

    skip, reason = _should_skip(fulcrum_t, regime,
                                fulcrum_t.fulcrum_price if fulcrum_t else current_price,
                                daily_vp)
    # Upgrade the opaque "no_trigger" with the descriptive reason built above (only set
    # when force_trigger was not used and fulcrum_t is None).
    if reason == "no_trigger" and force_trigger is None and _no_trig_reason:
        reason = _no_trig_reason
    # Proximity gate: reject a fulcrum sitting too far from current price (a stale /
    # far cached-HVN edge). Measured in the ANALYSIS frame, before any rebase. Such a
    # fulcrum both rebases to a garbage offset and places stops far off the venue's
    # market. Catches the stale-HVN bug the honest sim labeler exposed.
    if not skip and fulcrum_t is not None and current_price > 0 and max_fulcrum_dist_pct > 0:
        dist_pct = abs(fulcrum_t.fulcrum_price - current_price) / current_price
        if dist_pct > max_fulcrum_dist_pct:
            skip, reason = True, f"fulcrum_too_far:{dist_pct:.3f}>{max_fulcrum_dist_pct}"
    # Vol-compression gate/label: a neutral straddle only pays on expansion FROM a coil.
    # ALWAYS compute the label (squeeze_ok/rank) when there's a fulcrum — so an ungated run
    # still tags every arm for the squeeze A/B. ENFORCE (skip uncoiled) only when
    # require_squeeze_gate is set — else arm both and let the outcome audit compare.
    sq_ok, sq_rank = False, 1.0
    if not skip and fulcrum_t is not None:
        sq_ok, sq_rank = zone_triggers.squeeze_gate(symbol, tf, grid_cfg)
        # Structural HVN setups arm on a node edge/touch, not on a vol coil — exempt
        # them from the squeeze gate. hvn_edge reads the same daily/weekly VP the chart
        # draws, so it should arm on the visible edge-touch regardless of squeeze.
        _structural_kinds = ("hvn_inside_touch", "hvn_displacement", "hvn_edge")
        is_structural = ((fulcrum_t is not None and fulcrum_t.kind in _structural_kinds)
                         or any(k in trigger_hint for k in _structural_kinds))
        if bool(grid_cfg.get("require_squeeze_gate", False)) and not sq_ok and not is_structural:
            skip, reason = True, f"no_squeeze_gate:rank={sq_rank:.2f}"
    if skip:
        return GridPlan(verdict="skip", skip_reason=reason,
                        regime=getattr(regime, "type", "unknown"),
                        atr=round(atr, 4), plan_id=plan_id)

    fulcrum = fulcrum_t.fulcrum_price
    skew, skew_reason = _resolve_skew(fulcrum_t, regime, grid_cfg, symbol=symbol, tf=tf)
    swing_range = _opposing_swing_range(symbol, fulcrum, skew, daily_vp, atr)
    conviction = _candle_conviction(symbol, tf)
    n, step = _size_grid(fulcrum_t, regime, atr, swing_range, conviction,
                         hvn_max_legs=hvn_max_legs,
                         lvn_legs_per_side=lvn_legs_per_side,
                         mean_rev_step_mult=mean_rev_step_mult)
    # Freeze-aware floor: ensure the innermost leg (fulcrum ± step) clears the broker's
    # min-stop distance. min_step_venue is in the VENUE frame; convert to the analysis
    # frame here (× current/venue) since the step is rebased back by venue/current later.
    if min_step_venue > 0 and venue_price and venue_price > 0:
        min_step_analysis = min_step_venue * (current_price / venue_price)
        if step < min_step_analysis:
            step = round(min_step_analysis, 4)

    # Ladder-span gate: a neutral straddle only works if price sits INSIDE the ladder
    # [fulcrum − n·step, fulcrum + n·step]. If the fulcrum is a level price has already
    # run away from, the near-side stops land on the wrong side of market and the broker
    # (EA freeze guard) rejects them → a one-sided grid. The %-gate above is span-agnostic
    # (max_fulcrum_dist_pct 5% ≈ 3200pts at BTC vs a ~190pt ladder) so it can't catch this;
    # gate here on the ACTUAL span now that n·step is final. Analysis frame throughout.
    # candle_sweep is exempt: buy legs are ALL above candle_high, sell legs ALL below
    # candle_low — price at arm time is always between them (inside the candle's range).
    if fulcrum_t.kind != "candle_sweep" and current_price > 0 and n > 0 and step > 0:
        ladder_half = n * step
        off = abs(current_price - fulcrum)
        if off > ladder_half:
            return GridPlan(verdict="skip",
                            skip_reason=f"price_outside_ladder:{off:.4f}>{ladder_half:.4f}",
                            regime=getattr(regime, "type", "unknown"),
                            atr=round(atr, 4), plan_id=plan_id)

    # candle_sweep: buy legs anchor at candle_high, sell legs at candle_low (breakout grid).
    # Both sides built outward from their respective edges — not from a single shared fulcrum.
    if fulcrum_t.kind == "candle_sweep":
        _ch = float((fulcrum_t.context or {}).get("candle_high", 0.0))
        _cl = float((fulcrum_t.context or {}).get("candle_low",  0.0))
        _buy_lots  = _ladder(n, base_lot, lot_step, heavy_near_mid=False)
        _sell_lots = _ladder(n, base_lot, lot_step, heavy_near_mid=False)
        # First leg AT candle edge (i=0), subsequent legs spreading outward.
        buy_legs  = [Leg(price=round(_ch + i * step, 4), lot=_buy_lots[i])
                     for i in range(0, n)]
        sell_legs = [Leg(price=round(_cl - i * step, 4), lot=_sell_lots[i])
                     for i in range(0, n)]
    else:
        buy_legs, sell_legs = _build_legs(fulcrum, n, step, skew, base_lot, lot_step)

    # TP cascade — BTC Jun22 regime + min_tp_dist guard. STRUCTURE over ATR:
    #   1) base    = outer leg ± tp_atr_mult·ATR, snapped to the nearest strong structural
    #      zone beyond the outer leg (_resolve_tps → zone_collector zones, strength ≥ 0.6).
    #   2) override = the trigger's pre-computed structural target (tp_up/tp_down: HVN node
    #      edge or adjacent VP level) when it clears the outer leg by min_tp_dist — the
    #      guard against the tiny-TP-into-own-node bug (a target closer than min_tp_dist is
    #      ignored, keeping the ATR/zone base above).
    #   3) hvn_inside_touch reversion → POC on the FADE side (hvn_reversion_bias): tapped
    #      TOP fades DOWN to POC (sells), tapped BOTTOM reverts UP to POC (buys), only when
    #      POC clears the inner leg by min_tp_dist. Breakout side keeps its structural target.
    # Under net_profit_exit_only + leg_tp_ceiling these are a FAR CEILING; net_target is
    # the primary exit. (Dropped: VP 1×/2×step refinement, BB 2.5σ/mid, session extension.)
    top_leg = max((l.price for l in buy_legs),  default=fulcrum)
    bot_leg = min((l.price for l in sell_legs), default=fulcrum)
    min_tp_dist = float(grid_cfg.get("min_tp_dist", 0.0) or 0.0)

    buy_tp, sell_tp = _resolve_tps(symbol, fulcrum, buy_legs, sell_legs, atr, tp_mult)

    tp_up   = float((fulcrum_t.context or {}).get("tp_up", 0.0) or 0.0)
    tp_down = float((fulcrum_t.context or {}).get("tp_down", 0.0) or 0.0)
    if tp_up > top_leg + min_tp_dist:
        buy_tp = round(tp_up, 4)
    if 0.0 < tp_down < bot_leg - min_tp_dist:
        sell_tp = round(tp_down, 4)

    if (fulcrum_t.kind == "hvn_inside_touch"
            and bool(grid_cfg.get("hvn_reversion_bias", True))):
        poc  = float((daily_vp or {}).get("poc", 0.0) or 0.0)
        edge = (fulcrum_t.context or {}).get("edge", "")
        if poc > 0:
            if edge == "top" and poc < bot_leg - min_tp_dist:
                sell_tp = round(poc, 4)
            elif edge == "bottom" and poc > top_leg + min_tp_dist:
                buy_tp = round(poc, 4)

    # Structural SL for each setup.
    # hvn_inside_touch: SL is DEFERRED — monitor_cycle arms it once >50% of a side fills.
    # At placement time these stay 0; node_low/node_high are stored in the arm state for
    # monitor_cycle to read. This prevents early fills from getting stopped prematurely
    # before the trade has had a chance to commit direction.
    buy_sl = sell_sl = 0.0

    # candle_sweep: structural SL at arm time — buy stops below candle_low (the whole
    # sweep candle is the thesis; if price falls below its low the setup failed), sell stops
    # above candle_high. VWAP-BE then overrides these once the cycle moves ≥ candle_hl.
    if fulcrum_t.kind == "candle_sweep":
        _cs_ctx = fulcrum_t.context or {}
        _cs_hi = float(_cs_ctx.get("candle_high") or 0.0)
        _cs_lo = float(_cs_ctx.get("candle_low")  or 0.0)
        if _cs_hi > 0 and _cs_lo > 0:
            buy_sl  = round(_cs_lo, 4)   # buy stops: SL below sweep candle low
            sell_sl = round(_cs_hi, 4)   # sell stops: SL above sweep candle high

    # HVN displacement keeps ONLY its SL override (counter-side = displacement candle
    # extreme); its TP now follows the unified next-HVN-far-edge rule above.
    if fulcrum_t.kind == "hvn_displacement":
        ctx = fulcrum_t.context
        direction = ctx.get("direction", "")
        extreme = float(ctx.get("candle_extreme") or 0.0)
        if extreme > 0:
            if direction == "buy":
                buy_sl  = 0.0                 # bias side (buys): no hard SL — BE logic owns it
                sell_sl = round(extreme, 4)   # counter side (sells): SL = candle high
            elif direction == "sell":
                sell_sl = 0.0
                buy_sl  = round(extreme, 4)   # counter side (buys): SL = candle low

    # BB expansion touch: counter-side SL = BB mid (the fulcrum itself).
    # If price returns to the 20-SMA after touching the 2.5σ band, the directional
    # thesis has failed. Bias side has no hard SL — fullfill_be / bias_trail own exit.
    if fulcrum_t.kind == "bb_expansion_touch":
        bb_mid_sl = float(fulcrum_t.context.get("bb_mid") or 0.0)
        bias_dir  = fulcrum_t.context.get("bias", "")
        if bb_mid_sl > 0:
            if bias_dir == "sell":
                sell_sl = 0.0
                buy_sl  = round(bb_mid_sl, 4)   # counter (buys): SL if price reclaims BB mid
            elif bias_dir == "buy":
                buy_sl  = 0.0
                sell_sl = round(bb_mid_sl, 4)   # counter (sells): SL if price returns to BB mid

    # candle_sweep: compute VWAP-BE threshold (= candle_hl × lot × contract_size ≈ P&L
    # from moving one full candle range with the first filled leg) and store alongside the
    # VWAP price so monitor_cycle can arm the BE without needing to re-read settings.
    if fulcrum_t.kind == "candle_sweep":
        _cs_hl  = float((fulcrum_t.context or {}).get("candle_hl") or 0.0)
        _cs_vwap = float((fulcrum_t.context or {}).get("vwap") or 0.0)
        _contract = float(grid_cfg.get("candle_sweep_contract_size",
                           (settings.get("execution") or {}).get("contract_size", {}).get(symbol, 1.0) or 1.0))
        _be_usd = round(_cs_hl * base_lot * _contract, 2) if _cs_hl > 0 else 0.0
        fulcrum_t.context["sweep_be_usd"] = _be_usd
        fulcrum_t.context["sweep_vwap"]   = _cs_vwap

    plan = GridPlan(
        verdict="arm",
        fulcrum=round(fulcrum, 4),
        regime=getattr(regime, "type", "unknown"),
        regime_confidence=round(float(getattr(regime, "confidence", 0.0) or 0.0), 3),
        trigger_kind=fulcrum_t.kind,
        trigger_confidence=round(fulcrum_t.confidence, 3),
        squeeze_ok=bool(sq_ok),
        squeeze_rank=round(float(sq_rank), 3),
        n_per_side=n,
        step=step,
        skew=skew,
        skew_reason=skew_reason,
        buy_legs=buy_legs,
        sell_legs=sell_legs,
        buy_tp=buy_tp,
        sell_tp=sell_tp,
        buy_sl=buy_sl,
        sell_sl=sell_sl,
        atr=round(atr, 4),
        swing_range=round(swing_range, 4),
        plan_id=plan_id,
        analysis_anchor=round(current_price, 4),
        venue_anchor=round(current_price, 4),
        trigger_context=dict(fulcrum_t.context or {}),
        base_lot=base_lot,
        lot_step=lot_step,
    )

    # Re-anchor onto the execution venue's live price (if the EA supplied one).
    if venue_price and venue_price > 0:
        plan = _rebase_to_venue(plan, current_price, venue_price)
    return plan
