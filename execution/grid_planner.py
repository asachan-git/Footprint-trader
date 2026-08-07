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


# trigger-hint groups: a hint may name one kind, a comma list, or a group keyword.
# vp_level_touch intentionally EXCLUDED — disabled per user (HVN-inside-touch + squeeze
# only). The detector still runs but can never win the structural hint, so no VP-level
# straddles arm. Re-add "vp_level_touch" here to revive it.
_HINT_GROUPS = {
    "structural": {"hvn_inside_touch", "squeeze"},
    # LVN displacement: vp_level_touch is the only detector that arms on an LVN.
    # The planner narrows it to level_type=="lvn" (see plan_grid_levels) so only the
    # vacuum fires. Neutral straddle-in-vacuum: price sits in the LVN, leaves fast one
    # side, that leg fills + runs to the bounding HVN; the opposite leg never triggers.
    "lvn_displacement": {"vp_level_touch"},
    # LVN EDGE tap (ported from jul09 2026-08-06) — distinct from lvn_displacement above:
    # that straddles the vacuum MID (step=node_width/2), this arms ON an edge with
    # near-edge TPs into the next HVN. Separate detector, separate magic (strat 13 vs 3).
    "lvn_edge": {"lvn_edge_touch"},
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
    if (trigger.kind not in ("hvn_edge", "hvn_inside_touch", "vp_level_touch", "squeeze")
            and _price_inside_hvn(fulcrum, daily_vp)):
        return True, "chop:inside_hvn"
    if daily_vp and daily_vp.get("current_position") == "at_poc":
        # a POC / naked-POC fulcrum SHOULD arm at the POC — that's the setup, not chop.
        if not (trigger.kind == "vp_level_touch"
                and trigger.context.get("level_type") in ("poc", "naked_poc")):
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
               conviction: float, hvn_max_legs: int = 8,
               lvn_legs_per_side: int = 1, min_legs: int = 3) -> tuple[int, float]:
    """N (capped by regime.max_legs) and step ($) from ATR + swing range +
    candle conviction. HVN node width floors the step so legs span the node.

    min_legs (2026-08-05, user: "make min as 3") is a FLOOR applied after every cap,
    so it overrides regime.max_legs when that is lower (uncertain=2 → still 3). The LVN
    displacement path is exempt — its leg count is the deliberate lvn_legs_per_side and
    its step is node_width/2 (inner legs sit exactly on the vacuum edges); forcing 3
    there would push legs to ±1.5×width and break that geometry."""
    rtype = getattr(regime, "type", "uncertain")
    step_mult = TREND_STEP_MULT if rtype in ("trend_up", "trend_down") else MEAN_REV_STEP_MULT
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

    # HVN rotation / breakout-retest (hvn_inside_touch, hvn_edge): the ladder must FIT THE
    # NODE, because the fulcrum is a node edge and the far edge is the target.
    #
    # Leg count is still ATR-derived (node_width / 0.5·ATR, clamped) so a wider node — or a
    # calmer regime — still gets more legs. But the STEP is then re-derived from the node so
    # the ladder actually spans it: step = node_width / (n+1). The +1 leaves the outermost
    # leg exactly one step short of the far edge, so the far edge stays a TARGET rather than
    # becoming an entry — which is the whole rotate-across-the-node-then-break thesis. Same
    # pattern the LVN path above already uses (node_width / 2 puts its inner legs on the
    # vacuum edges).
    #
    # Why (2026-08-06): step was a pure ATR multiple, and on 15m ATR (8.51) can exceed the
    # node itself. Measured live — ladder span as a % of node width:
    #     1m  98% / 100%      5m  108% / 92%      15m  125% / 106% / 213%
    # A 213% span puts every leg in the vacuum OUTSIDE the node, so nothing straddles the
    # structure, and the far edge (the TP) ends up INSIDE the ladder — which fails the
    # `tp_up > top_leg` guard and silently drops the structural TP back to the flat ATR
    # fallback. It also drove the 183 price_outside_ladder skips. With this, span lands at a
    # consistent 67-86% of node width on every TF.
    if trigger.kind in ("hvn_inside_touch", "hvn_edge", "lvn_edge_touch"):
        _atr_step = (step_mult * atr) if atr > 0 else max(trigger.raw_range / 8.0, 1e-6)
        n = int(round(trigger.raw_range / _atr_step)) if _atr_step > 0 else 2
        n = max(min_legs, min(hvn_max_legs, n))
        # Fit the node. Fall back to the ATR step when the trigger carries no width.
        step = (trigger.raw_range / (n + 1)) if trigger.raw_range > 0 else _atr_step
        return n, round(max(step, 1e-6), 4)

    step = step_mult * atr if atr > 0 else max(trigger.raw_range / 4.0, 1e-6)

    # HVN node width: ensure legs straddle the whole node, not a sliver of it.
    if trigger.raw_range > 0:
        step = max(step, trigger.raw_range / 4.0)
    if step <= 0:
        step = max(swing_range / 5.0, 1e-6)

    legs_to_cover = int(swing_range / step) if step > 0 else max_legs
    n = max(min_legs, min(max_legs, legs_to_cover))
    # conviction scales N upward toward the cap.
    n = max(min_legs, min(max_legs, int(round(n * (0.7 + 0.6 * conviction)))))
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

def _ladder(n: int, base_lot: float, lot_step: float) -> list[float]:
    """LINEAR: lot size increases with distance from the fulcrum — index 1 (nearest)
    gets base_lot, each leg outward adds lot_step. ALWAYS increasing outward as of
    2026-08-05 (user) — previously flipped to LINEAR_REVERSED (heaviest nearest the
    fulcrum, tapering outward) for whichever side matched the current skew, mirroring
    the EA's ResolveLotForIndex. User flagged that as backwards for the buy side and
    asked for a single consistent increasing-with-distance shape on both sides."""
    lots = []
    for i in range(1, n + 1):
        raw = base_lot + (i - 1) * lot_step
        lots.append(round(max(base_lot, raw), 2))
    return lots


def _build_legs(fulcrum: float, n: int, step: float, skew: str,
                base_lot: float, lot_step: float) -> tuple[list[Leg], list[Leg]]:
    """fulcrum ± i·step prices. Favoured side gets one extra leg (skew); both sides'
    lot ladders always increase with distance from the fulcrum (see _ladder)."""
    buy_n = n
    sell_n = n
    # Skew adds one extra leg to the favoured side. (Used to ALSO flip that side's
    # ladder to heaviest-near-fulcrum — removed 2026-08-05, see _ladder.)
    if skew == "buy":
        buy_n = n + 1
    elif skew == "sell":
        sell_n = n + 1

    buy_lots = _ladder(buy_n, base_lot, lot_step)
    sell_lots = _ladder(sell_n, base_lot, lot_step)

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
    hvn_max_legs = int(grid_cfg.get("hvn_max_legs", 8))
    lvn_legs_per_side = int(grid_cfg.get("lvn_legs_per_side", 1))
    min_legs = int(grid_cfg.get("min_legs_per_side", 3))
    max_fulcrum_dist_pct = float(grid_cfg.get("max_fulcrum_dist_pct", 0.05))

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
                                        atr=atr, daily_vp=daily_vp, cfg=grid_cfg)
    hint_set = _hint_set(trigger_hint)
    if hint_set:
        hinted = [t for t in triggers if t.kind in hint_set]
        if hinted:
            triggers = hinted
    # lvn_displacement narrows vp_level_touch to the VACUUM only (LVN), excluding
    # POC/VA/naked-POC taps — those are mean-revert lines, not displacement vacuums.
    if trigger_hint == "lvn_displacement":
        lvn_only = [t for t in triggers if t.context.get("level_type") == "lvn"]
        triggers = lvn_only
    # vp_levels is the MIRROR of the above: POC/VAH/VAL/naked-POC mean-revert lines only,
    # LVN excluded. Both hints share the vp_level_touch detector and vp_fulcrum_levels
    # (which lists lvn), so without this an LVN tap would arm TWICE — once here under the
    # vp_levels setup magic (strat 9) and again under lvn_displacement's (strat 3) —
    # doubling exposure on one level. va triggers are unaffected (no level_type).
    elif trigger_hint == "vp_levels":
        triggers = [t for t in triggers
                    if t.context.get("level_type") != "lvn"]

    # force_trigger (2026-08-05): the intrabar touch-arm path resolves the tapped HVN edge
    # itself and injects that Trigger directly, bypassing the winner-take-all
    # _score_and_pick. Without this a real tap could be silently beaten by a same-poll
    # imbalance/anchor trigger and vanish as trigger_mismatch. Everything downstream
    # (gates, sizing, TP cascade, venue rebase) is untouched and runs identically.
    fulcrum_t = (force_trigger if force_trigger is not None
                 else _score_and_pick(triggers, regime, current_price, daily_vp, bars))

    # 2026-08-05 (user, "remove the restrictions of any kind"): a hard kill-switch for
    # every entry-blocking gate below (chop/at-poc, fulcrum-too-far, ladder-span,
    # require_squeeze_gate enforcement). Does NOT touch position sizing, targets, trail,
    # or touch_arm_confirm_ticks — those are separate, deliberately-set mechanisms.
    _no_gates = bool(grid_cfg.get("disable_entry_gates", False))
    if fulcrum_t is None:
        # NOT a gate — there is simply nothing to arm. _should_skip owns this check, so
        # short-circuiting it with disable_entry_gates dropped the None guard too and
        # execution ran on to `fulcrum_t.fulcrum_price` → AttributeError, a hard 500 on
        # /exec/emit_grid (observed live 2026-08-06 04:0x). Checked before the flag.
        skip, reason = True, "no_trigger"
    else:
        skip, reason = (False, "") if _no_gates else _should_skip(
            fulcrum_t, regime, fulcrum_t.fulcrum_price, daily_vp)
    # Proximity gate: reject a fulcrum sitting too far from current price (a stale /
    # far cached-HVN edge). Measured in the ANALYSIS frame, before any rebase. Such a
    # fulcrum both rebases to a garbage offset and places stops far off the venue's
    # market. Catches the stale-HVN bug the honest sim labeler exposed.
    if not _no_gates and not skip and fulcrum_t is not None and current_price > 0 and max_fulcrum_dist_pct > 0:
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
        if bool(grid_cfg.get("require_squeeze_gate", False)) and not sq_ok:
            skip, reason = True, f"no_squeeze_gate:rank={sq_rank:.2f}"
    if skip:
        return GridPlan(verdict="skip", skip_reason=reason,
                        regime=getattr(regime, "type", "unknown"),
                        atr=round(atr, 4), plan_id=plan_id)

    fulcrum = fulcrum_t.fulcrum_price
    skew, skew_reason = _resolve_skew(fulcrum_t, regime, grid_cfg, symbol=symbol, tf=tf)
    # 2026-08-05 (user: "do not use the skew, place neutral grid only"). Kept the vote
    # computation above so skew_reason/skew_votes still land in the audit log for study —
    # only the ACTING on it is suppressed: _build_legs no longer gets an extra leg on one
    # side, so buy_n == sell_n and the ladder is symmetric about the fulcrum.
    if not bool(grid_cfg.get("skew_enabled", True)):
        skew_reason = f"neutral_forced (would have been {skew or 'none'}: {skew_reason})"
        skew = "none"
    swing_range = _opposing_swing_range(symbol, fulcrum, skew, daily_vp, atr)
    conviction = _candle_conviction(symbol, tf)
    n, step = _size_grid(fulcrum_t, regime, atr, swing_range, conviction,
                         min_legs=min_legs,
                         hvn_max_legs=hvn_max_legs,
                         lvn_legs_per_side=lvn_legs_per_side)
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
    #
    # 2026-08-05: deliberately NOT covered by disable_entry_gates. That flag turns off the
    # discretionary filters (chop/at-poc, fulcrum-too-far); this one is geometric, not a
    # preference — it only ever rejects a ladder that CANNOT bracket price, so it can never
    # block a legitimate straddle. It fired 16× on 2026-08-05 with ratios like 29.6>2.91 and
    # 87.9>8.77 (price 10× the ladder half-span away from the fulcrum) — i.e. every leg of a
    # "neutral" straddle landing on one side of market. Opt out via disable_ladder_span_gate.
    # CENTRE-PROXIMITY (2026-08-06, user): containment alone is too weak. `off <= n*step`
    # permits price to sit ON the outermost leg and still arm — which is not a straddle, it
    # is a one-sided grid. Observed live: magic 775052 armed at 12:15:01 with fulcrum
    # 4262.30, step 2.00, n=3 (half-span 6.00) while price was ~4257 — off 5.3, so it passed
    # by 0.7. Result: all three buys sat 7-12pt overhead (unreachable) and the sell side
    # straddled the market instead of the fulcrum (2 legs flipped to sell_limit).
    # ladder_center_frac scopes the allowed offset to the INNER fraction of the half-span,
    # so price must genuinely be near the decision point. 1.0 restores the old behaviour.
    _center_frac = float(grid_cfg.get("ladder_center_frac", 0.5) or 1.0)
    if (not bool(grid_cfg.get("disable_ladder_span_gate", False))
            and current_price > 0 and n > 0 and step > 0):
        ladder_half = n * step
        allowed = ladder_half * max(0.0, min(1.0, _center_frac))
        off = abs(current_price - fulcrum)
        if off > allowed:
            return GridPlan(verdict="skip",
                            skip_reason=(f"price_outside_ladder:{off:.4f}>{allowed:.4f}"
                                         f"(half={ladder_half:.2f}x{_center_frac:g})"),
                            regime=getattr(regime, "type", "unknown"),
                            atr=round(atr, 4), plan_id=plan_id)

    buy_legs, sell_legs = _build_legs(fulcrum, n, step, skew, base_lot, lot_step)
    buy_tp, sell_tp = _resolve_tps(symbol, fulcrum, buy_legs, sell_legs, atr, tp_mult)

    # Structural TP override: any trigger that pre-computed tp_up/tp_down (HVN
    # node edges for hvn_inside_touch; adjacent VP levels for vp_level_touch) targets
    # that structure. Guard: the target must lie BEYOND the OUTER leg, else it would
    # sit inside the ladder and the grid could never profit — in that case keep the
    # _resolve_tps()/ATR fallback already computed above.
    tp_up = float(fulcrum_t.context.get("tp_up", 0.0) or 0.0)
    tp_down = float(fulcrum_t.context.get("tp_down", 0.0) or 0.0)
    if tp_up or tp_down:
        top_leg = max((l.price for l in buy_legs), default=fulcrum)
        bot_leg = min((l.price for l in sell_legs), default=fulcrum)
        if tp_up > top_leg:
            buy_tp = round(tp_up, 4)
        if 0.0 < tp_down < bot_leg:
            sell_tp = round(tp_down, 4)

    # HVN inside-touch reversion TP: the fade INTO the node targets the POC (the node's
    # acceptance magnet), not the far next-HVN. Tapped TOP → sells fade DOWN to POC;
    # tapped BOTTOM → buys revert UP to POC. Breakout side keeps its far next-HVN target
    # (tp_up/tp_down above). Only when POC sits beyond the inner leg on the fade side
    # (else it'd sit inside the ladder → keep the structural/ATR target).
    if (fulcrum_t.kind == "hvn_inside_touch"
            and bool(grid_cfg.get("hvn_reversion_bias", True))):
        poc = float((daily_vp or {}).get("poc", 0.0) or 0.0)
        edge = fulcrum_t.context.get("edge", "")
        bot_leg = min((l.price for l in sell_legs), default=fulcrum)
        top_leg = max((l.price for l in buy_legs), default=fulcrum)
        if poc > 0:
            if edge == "top" and poc < bot_leg:        # fade down to POC (sell side)
                sell_tp = round(poc, 4)
            elif edge == "bottom" and poc > top_leg:   # revert up to POC (buy side)
                buy_tp = round(poc, 4)

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
        atr=round(atr, 4),
        swing_range=round(swing_range, 4),
        plan_id=plan_id,
        analysis_anchor=round(current_price, 4),
        venue_anchor=round(current_price, 4),
        trigger_context=dict(fulcrum_t.context or {}),
    )

    # Re-anchor onto the execution venue's live price (if the EA supplied one).
    if venue_price and venue_price > 0:
        plan = _rebase_to_venue(plan, current_price, venue_price)
    return plan
