"""Sanity gates on Decision / GridDecision before logging / executing.

validate()          — original single-entry Decision schema
validate_grid_plan() — new GridDecision schema (Phase 7, v4 prompt)
"""

from __future__ import annotations

from .schema import Decision


MIN_RISK_PCT = 0.0005   # SL must be ≥ 0.05% of entry price (XAUT ~2.5pts, BTC ~38pts)
MIN_LEG_SEPARATION_ATR = 0.30  # adjacent legs must be ≥ 0.30 × ATR apart


def validate(decision: Decision, min_rationale_len: int = 20) -> str | None:
    """Returns None if valid, else a short reason."""
    if len(decision.rationale) < min_rationale_len and decision.side != "flat":
        return f"rationale too short ({len(decision.rationale)} chars) — Claude must cite footprint features"
    if decision.side == "flat":
        return None
    if decision.entry is None or decision.stop_loss is None or decision.take_profit is None:
        return "missing entry/SL/TP"
    entry, sl, tp = decision.entry, decision.stop_loss, decision.take_profit
    if decision.side == "long":
        if sl >= entry:
            return "long: SL not below entry"
        if tp <= entry:
            return "long: TP not above entry"
        risk = entry - sl
        reward = tp - entry
    else:  # short
        if sl <= entry:
            return "short: SL not above entry"
        if tp >= entry:
            return "short: TP not below entry"
        risk = sl - entry
        reward = entry - tp
    if risk <= 0:
        return "risk <= 0"
    min_risk = entry * MIN_RISK_PCT
    if risk < min_risk:
        return f"SL too tight: risk {risk:.2f} < min {min_risk:.2f} ({MIN_RISK_PCT*100:.3f}% of entry)"
    return None


def validate_grid_plan(
    gd,                         # GridDecision
    current_price: float,
    atr: float,
    max_lots: float = 0.50,
) -> str | None:
    """Validate a GridDecision returned by Claude (v4 submit_grid_plan).

    Returns None if valid, else a short rejection reason.
    """
    from .schema import GridDecision  # local import to avoid circular

    if gd.side == "flat":
        return None  # flat is always valid

    if not gd.grid_orders:
        return "non-flat side with empty grid_orders"

    pm = gd.position_management
    if pm is None:
        return "missing position_management"

    tp = pm.tp_primary
    sl = pm.sl

    # TP and SL on correct side of current price
    if gd.side == "long":
        if sl >= current_price:
            return f"long: SL {sl} not below current price {current_price}"
        if tp <= current_price:
            return f"long: TP {tp} not above current price {current_price}"
        if tp - current_price < atr * 1.5:
            return f"long: TP {tp} closer than 1.5×ATR ({atr*1.5:.2f}) from entry"
    else:
        if sl <= current_price:
            return f"short: SL {sl} not above current price {current_price}"
        if tp >= current_price:
            return f"short: TP {tp} not below current price {current_price}"
        if current_price - tp < atr * 1.5:
            return f"short: TP {tp} closer than 1.5×ATR ({atr*1.5:.2f}) from entry"

    # Lots cap
    total_qty = sum(leg.qty for leg in gd.grid_orders)
    if total_qty > max_lots:
        return f"total_qty {total_qty:.3f} exceeds max_lots {max_lots}"

    # Leg separation — adjacent legs must be at least MIN_LEG_SEPARATION_ATR apart
    if len(gd.grid_orders) > 1:
        prices = sorted(leg.price for leg in gd.grid_orders)
        min_sep = atr * MIN_LEG_SEPARATION_ATR
        for i in range(len(prices) - 1):
            if prices[i + 1] - prices[i] < min_sep:
                return f"legs too close: {prices[i]:.2f} and {prices[i+1]:.2f} < {min_sep:.2f} apart"

    # Entries must be at-or-better than current price
    for leg in gd.grid_orders:
        if gd.side == "long" and leg.price > current_price * 1.002:
            return f"long leg {leg.leg_idx} price {leg.price} is above current {current_price} (chasing)"
        if gd.side == "short" and leg.price < current_price * 0.998:
            return f"short leg {leg.leg_idx} price {leg.price} is below current {current_price} (chasing)"

    # Max legs cap (counter-trend enforcement already done upstream via decision_validator)
    if pm.max_legs > 5:
        return f"max_legs {pm.max_legs} exceeds hard cap 5"

    return None
