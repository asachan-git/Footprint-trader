"""Grid placer v2 — zone-based 5-leg limit-order grid.

Leg prices placed at IMPORTANT ZONES (HVN, FVG, VP levels, swing pivots)
ranked by confluence strength. Falls back to ATR-step if fewer than 5
zones found on the correct side.

Lot ladder: Fib [1,1,2,3,5] × BASE_LOT × (bias_strength/5).
Nearest zone gets smallest lot (leg1=1), furthest gets biggest (leg5=5).
This preserves optimal avg-entry pricing while targeting institutional levels.

No static SL on legs. Cycle invalidation via ChoCh / VAH-VAL break
monitored by cycle_manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


FIB_LADDER = (1, 1, 2, 3, 5)
N_LEGS = 5


@dataclass(frozen=True)
class GridLegPlan:
    leg_idx: int
    price: float
    lots: float
    side: Literal["long", "short"]
    source: str  # zone source label


@dataclass(frozen=True)
class GridPlan:
    symbol: str
    broker_symbol: str
    side: Literal["long", "short"]
    legs: list[GridLegPlan]
    avg_entry_on_full_fill: float
    take_profit: float
    tp_source: str
    bias_strength: int
    safety_sl: float | None
    note: str
    # NEW: scale-free view, populated by plan_grid for cross-venue translation
    anchor_price: float = 0.0
    leg_offsets_pct: tuple = ()      # tuple[float, ...] of (price - anchor) / anchor per leg
    tp_offset_pct: float = 0.0       # (tp - anchor) / anchor
    safety_sl_offset_pct: float | None = None


@dataclass(frozen=True)
class NormalizedGridLeg:
    leg_idx: int
    offset_pct: float          # signed % from anchor (negative = below)
    lots: float
    source: str


@dataclass(frozen=True)
class NormalizedGridPlan:
    """Venue-neutral grid plan. Use venue_translator to materialize on a specific venue."""
    symbol: str                # analysis-venue symbol
    side: Literal["long", "short"]
    anchor_price: float        # analysis-venue close at decision time (reference only)
    legs: list[NormalizedGridLeg]
    tp_offset_pct: float       # signed
    tp_source: str
    bias_strength: int
    safety_sl_offset_pct: float | None
    note: str


def _fallback_leg_prices(
    direction: Literal["long", "short"],
    anchor: float,
    atr_15m: float,
    step_mult: float,
    n: int,
) -> list[tuple[float, str]]:
    """ATR-step fallback when not enough zones available."""
    step = step_mult * atr_15m
    prices = []
    for i in range(n):
        if direction == "long":
            prices.append((anchor - (i + 1) * step, f"atr_step_{i+1}"))
        else:
            prices.append((anchor + (i + 1) * step, f"atr_step_{i+1}"))
    return prices


def _resolve_tp_with_fallback(
    symbol: str,
    direction: Literal["long", "short"],
    tp_anchor: float,
    atr: float,
    htf_bars: list[Bar],
    min_distance: float,
    fallback_atr_mult: float = 3.0,
) -> tuple[float, str]:
    from execution.tp_resolver import resolve_tp
    cand = resolve_tp(symbol, direction, anchor=tp_anchor, htf_bars=htf_bars, min_distance=min_distance)
    if cand is not None:
        return cand.price, cand.source
    if direction == "long":
        return tp_anchor + fallback_atr_mult * atr, "fallback_atr"
    return tp_anchor - fallback_atr_mult * atr, "fallback_atr"


def plan_grid(
    *,
    symbol: str,
    broker_symbol: str,
    direction: Literal["long", "short"],
    anchor: float,
    bias_strength: int,
    atr_15m: float,
    base_lot: float,
    htf_bars: list[Bar] | None = None,
    step_mult: float = 0.5,
    min_tp_distance_mult: float = 0.5,
    safety_sl_atr_mult: float | None = 5.0,
) -> GridPlan:
    """Build a 5-leg zone-based grid plan.

    Leg prices: nearest important zones on the correct side of anchor.
    Lots: Fib [1,1,2,3,5] × base_lot × (bias_strength/5).
    TP: nearest VP/FVG/swing level beyond avg_entry_on_full_fill.
    """
    if not 1 <= bias_strength <= 5:
        raise ValueError(f"bias_strength must be 1..5, got {bias_strength}")
    if atr_15m <= 0:
        raise ValueError(f"atr_15m must be > 0, got {atr_15m}")

    scale = bias_strength / 5.0

    # ── Regime-aware leg count + spacing + safety SL ──────────────────────
    try:
        from pipeline.features.day_type import get_regime, grid_shape_for_regime
        regime = get_regime(symbol)
        shape = grid_shape_for_regime(regime, direction)
        n_legs = int(shape["n_legs"])
        step_mult = float(shape["step_mult"])
        grid_mode_label = shape["mode"]
        # Override safety_sl_atr_mult only if caller used default (5.0)
        if safety_sl_atr_mult == 5.0:
            safety_sl_atr_mult = float(shape["safety_sl_atr_mult"])
        regime_label = f"{regime.type}({regime.confidence:.2f})"
    except Exception:
        n_legs = N_LEGS
        grid_mode_label = "mean_reversion"
        regime_label = "regime_unknown"

    # ── Collect zones ──────────────────────────────────────────────────────
    # Confluence-driven grid: request 2× legs from zone_collector so we have
    # candidates after dropping anchor-adjacent and weak ones.
    min_gap = max(atr_15m * 0.10, 0.1)
    try:
        from execution.zone_collector import collect as _collect
        zones = _collect(symbol, direction, anchor, htf_bars=htf_bars or [],
                         n=max(n_legs * 2, 8), min_gap=min_gap)
    except Exception:
        zones = []

    zone_prices: list[tuple[float, str]] = [(z.price, z.source) for z in zones]

    # Confluence-only mode: legs MUST land on collected zones (VP/swing/FVG/sweep).
    # No ATR-step fallback — better to run fewer legs than place legs at noise
    # prices that have no structural support.
    zone_prices.sort(key=lambda x: abs(x[0] - anchor))
    # Drop any zone too close to anchor (entry already there) — needs distance ≥ min_gap
    zone_prices = [zp for zp in zone_prices if abs(zp[0] - anchor) >= min_gap]
    if not zone_prices:
        # Last-resort safety: at least one zone is the anchor itself (leg-1)
        zone_prices = [(anchor, "anchor_fallback")]
    # Take up to n_legs strongest confluence zones (already nearest-first after sort)
    zone_prices = zone_prices[:n_legs]
    actual_n_legs = len(zone_prices)
    n_legs = actual_n_legs

    # ── Build legs ─────────────────────────────────────────────────────────
    # Use first n_legs entries of Fib ladder. For 2-leg cautious: [1,1].
    # For 3-leg directional: [1,1,2]. For 5-leg range: [1,1,2,3,5].
    ladder = FIB_LADDER[:n_legs]
    legs: list[GridLegPlan] = []
    weighted_price = 0.0
    weighted_units = 0
    for i, (mult, (price, source)) in enumerate(zip(ladder, zone_prices)):
        lots = round(base_lot * mult * scale, 2) or 0.01
        legs.append(GridLegPlan(leg_idx=i + 1, price=price, lots=lots,
                                side=direction, source=source))
        weighted_price += price * mult
        weighted_units += mult

    avg_entry = weighted_price / weighted_units

    # ── TP — place FAR initially so cycle_manager shrinker has room ────────
    # In RANGE mode: TP at next major HVN/POC (minimum distance = 1.5×ATR)
    # In TREND mode (with trend): TP at next HTF FVG / swept level (1.5×ATR)
    # In CAUTIOUS: tight TP (0.5×ATR) to lock quick exits
    if grid_mode_label == "directional":
        # Push TP further — trending move can extend
        tp_min_mult = max(min_tp_distance_mult, 1.5)
    elif grid_mode_label == "cautious":
        tp_min_mult = min(min_tp_distance_mult, 0.5)
    else:
        # Range / default — moderate TP, shrinker will tighten as legs fill
        tp_min_mult = max(min_tp_distance_mult, 1.5)
    # Anchor TP off leg1 price (the immediate fill), not avg_entry.
    # avg_entry assumes all 5 legs filled, but leg1 anchor fills first — if TP
    # is closer to leg1 than min_distance, anchor opens with TP <= entry.
    leg1_price = legs[0].price
    tp_price, tp_source = _resolve_tp_with_fallback(
        symbol=symbol, direction=direction, tp_anchor=leg1_price, atr=atr_15m,
        htf_bars=htf_bars or [], min_distance=tp_min_mult * atr_15m,
    )

    # ── Safety SL — disaster floor only ────────────────────────────────────
    # Disaster floor: ≥ 3 % for BTC, ≥ 1.5 % for XAU (catches news gaps).
    # Cycle should normally exit via TP / absorption / invalidation, not SL.
    safety_sl: float | None = None
    if safety_sl_atr_mult and safety_sl_atr_mult > 0:
        leg5_price = legs[-1].price
        atr_offset = safety_sl_atr_mult * atr_15m
        floor_pct  = 0.03 if symbol.startswith("BTC") else 0.015
        floor_off  = anchor * floor_pct
        offset = max(atr_offset, floor_off)   # whichever is farther
        safety_sl = (leg5_price - offset if direction == "long" else leg5_price + offset)

    sources_summary = ", ".join(f"leg{l.leg_idx}@{l.price:.2f}({l.source})" for l in legs)

    # Scale-free view — % offsets from anchor for cross-venue translation
    leg_offsets_pct = tuple((l.price - anchor) / anchor if anchor > 0 else 0.0 for l in legs)
    tp_offset_pct = (tp_price - anchor) / anchor if anchor > 0 else 0.0
    safety_sl_offset_pct = (
        (safety_sl - anchor) / anchor if (safety_sl is not None and anchor > 0) else None
    )

    return GridPlan(
        symbol=symbol, broker_symbol=broker_symbol,
        side=direction, legs=legs,
        avg_entry_on_full_fill=avg_entry,
        take_profit=tp_price, tp_source=tp_source,
        bias_strength=bias_strength, safety_sl=safety_sl,
        note=(f"regime={regime_label} mode={grid_mode_label} legs={n_legs} "
              f"bias={bias_strength}/5 avg={avg_entry:.2f} tp={tp_price:.2f}({tp_source}) | {sources_summary}"),
        anchor_price=anchor,
        leg_offsets_pct=leg_offsets_pct,
        tp_offset_pct=tp_offset_pct,
        safety_sl_offset_pct=safety_sl_offset_pct,
    )


def to_normalized(plan: GridPlan) -> NormalizedGridPlan:
    """Project a venue-bound GridPlan into a venue-neutral NormalizedGridPlan."""
    n_legs = [
        NormalizedGridLeg(
            leg_idx=l.leg_idx,
            offset_pct=plan.leg_offsets_pct[i] if i < len(plan.leg_offsets_pct) else 0.0,
            lots=l.lots, source=l.source,
        )
        for i, l in enumerate(plan.legs)
    ]
    return NormalizedGridPlan(
        symbol=plan.symbol, side=plan.side,
        anchor_price=plan.anchor_price,
        legs=n_legs,
        tp_offset_pct=plan.tp_offset_pct,
        tp_source=plan.tp_source,
        bias_strength=plan.bias_strength,
        safety_sl_offset_pct=plan.safety_sl_offset_pct,
        note=plan.note,
    )
