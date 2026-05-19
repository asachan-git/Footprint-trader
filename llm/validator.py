"""Sanity gates on Decision before logging / executing.

Reject (return reason string) if:
- side != flat but SL/TP/entry missing
- R:R below risk.yaml floor
- SL on wrong side of entry
"""

from __future__ import annotations

from .schema import Decision


MIN_RISK_PCT = 0.0005   # SL must be ≥ 0.05% of entry price (XAUT ~2.5pts, BTC ~38pts)


def validate(decision: Decision, rr_floor: float = 1.5, min_rationale_len: int = 20) -> str | None:
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
        return f"SL too tight: risk {risk:.4f} < min {min_risk:.4f} ({MIN_RISK_PCT*100:.3f}% of entry)"
    rr = reward / risk
    if rr < rr_floor:
        return f"R:R {rr:.2f} below floor {rr_floor}"
    return None
