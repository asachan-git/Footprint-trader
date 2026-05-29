"""Venue translator — converts a scale-free NormalizedGridPlan into a
broker-bound GridPlan using live execution-venue quote.

Analysis happens on Bybit/Binance (free, deep tick data).
Execution happens on Vantage MT5 via MetaApi (where the money lives).
The two venues have different absolute prices (spread, broker markup, feed
lag) but the SAME structural % move. We use Bybit's % offsets and rebase
them on Vantage's current mid.

Tick-size rounding + min-distance compliance applied per leg.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Literal

from execution.grid_placer import GridPlan, GridLegPlan, NormalizedGridPlan

LOG = logging.getLogger(__name__)


def _round_to_tick(price: float, tick_size: float) -> float:
    """Snap price to nearest tick multiple."""
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 8)


def _ensure_min_distance(
    price: float, anchor: float, tick_size: float, min_distance: float | None,
    side: Literal["long", "short"], away_from_anchor: bool = True,
) -> float:
    """Ensure price is at least `min_distance` from anchor in expected direction.
    `away_from_anchor`: True if this is a limit-order leg (away from current); False for TP (also away).
    """
    if not min_distance or min_distance <= 0:
        return price
    # For both long & short limits, legs sit away from anchor (above for short, below for long).
    # We just enforce |price - anchor| >= min_distance.
    if abs(price - anchor) >= min_distance:
        return price
    # Push out to the minimum
    if side == "long":
        return _round_to_tick(anchor - min_distance, tick_size)
    return _round_to_tick(anchor + min_distance, tick_size)


def translate(
    plan: NormalizedGridPlan,
    venue_quote: dict,
    broker_symbol: str,
    side: Literal["long", "short"],
) -> GridPlan:
    """Materialize a NormalizedGridPlan into a broker-bound GridPlan.

    venue_quote: result of MT5Adapter.get_quote() — dict with bid, ask, mid,
                 tick_size, min_distance, ok, error.

    Strategy:
      - anchor = venue mid (or last close if quote unavailable, with a warning)
      - For each leg: price = anchor × (1 + leg.offset_pct), rounded to tick
      - TP: anchor × (1 + tp_offset_pct), rounded
      - Safety SL: anchor × (1 + safety_sl_offset_pct) if set
      - Apply broker min_distance to legs (push out if too close)
    """
    if not venue_quote.get("ok"):
        LOG.warning(f"[venue_translator] quote unavailable for {broker_symbol} "
                    f"({venue_quote.get('error')}); falling back to plan.anchor_price")
        anchor = plan.anchor_price
    else:
        anchor = float(venue_quote["mid"])

    tick_size = float(venue_quote.get("tick_size") or 0.01)
    min_distance = venue_quote.get("min_distance")   # price units; None ok

    abs_legs: list[GridLegPlan] = []
    leg_offsets_pct = []
    for nleg in plan.legs:
        raw_price = anchor * (1.0 + nleg.offset_pct)
        snapped = _round_to_tick(raw_price, tick_size)
        snapped = _ensure_min_distance(snapped, anchor, tick_size, min_distance, side)
        abs_legs.append(GridLegPlan(
            leg_idx=nleg.leg_idx, price=snapped, lots=nleg.lots,
            side=side, source=nleg.source,
        ))
        leg_offsets_pct.append((snapped - anchor) / anchor if anchor > 0 else 0.0)

    tp_price = _round_to_tick(anchor * (1.0 + plan.tp_offset_pct), tick_size)
    safety_sl = None
    if plan.safety_sl_offset_pct is not None:
        safety_sl = _round_to_tick(anchor * (1.0 + plan.safety_sl_offset_pct), tick_size)

    avg_entry = (
        sum(l.price * l.lots for l in abs_legs) / sum(l.lots for l in abs_legs)
        if abs_legs and sum(l.lots for l in abs_legs) > 0 else anchor
    )

    note = (f"[venue-translated] venue_anchor={anchor:.2f} "
            f"(quote_ok={venue_quote.get('ok')}, tick={tick_size}, min_dist={min_distance}) | "
            f"{plan.note}")

    return GridPlan(
        symbol=plan.symbol, broker_symbol=broker_symbol,
        side=side, legs=abs_legs,
        avg_entry_on_full_fill=avg_entry,
        take_profit=tp_price, tp_source=plan.tp_source,
        bias_strength=plan.bias_strength, safety_sl=safety_sl,
        note=note,
        anchor_price=anchor,
        leg_offsets_pct=tuple(leg_offsets_pct),
        tp_offset_pct=plan.tp_offset_pct,
        safety_sl_offset_pct=plan.safety_sl_offset_pct,
    )


def fetch_venue_quote(broker: str, broker_symbol: str) -> dict:
    """Get live quote from execution venue. Returns the same dict shape as MT5Adapter.get_quote."""
    if broker == "vantage_mt5":
        try:
            from execution.live.mt5_adapter import MT5Adapter
            return MT5Adapter().get_quote(broker_symbol)
        except Exception as e:
            LOG.warning(f"[venue_translator] MT5 quote fetch failed: {e}")
            return {"ok": False, "error": str(e), "bid": None, "ask": None,
                    "mid": None, "tick_size": 0.01, "min_distance": None}
    # paper / journal / future brokers: fall through to "no quote"
    return {"ok": False, "error": "broker has no live quote", "bid": None,
            "ask": None, "mid": None, "tick_size": 0.01, "min_distance": None}
