"""Regime helpers — fetch day_type for a symbol and apply trade gating.

Used by:
  - execution/router.py     (regime gate: block trades against confirmed trends)
  - execution/grid_placer.py (regime-aware leg count + spacing)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.features.day_type import classify, DayType


@dataclass(frozen=True)
class RegimeView:
    type: str            # "range" | "trend_up" | "trend_down" | "uncertain"
    confidence: float
    max_legs: int
    grid_mode: str       # "mean_reversion" | "directional_long" | "directional_short" | "cautious"
    reason: str


_NULL = RegimeView(type="uncertain", confidence=0.0, max_legs=2,
                   grid_mode="cautious", reason="no_data")


def get_regime(symbol: str, primary_tf: str = "1m", session_bars_n: int = 100) -> RegimeView:
    """Compute day_type for the symbol from current session bars."""
    try:
        from pipeline.state_store import store
        from pipeline.features.vp_cache import get as vp_get
        bars = store().recent(symbol, primary_tf, session_bars_n)
        if not bars:
            return _NULL
        daily_vp = vp_get(symbol, "daily")
        dt = classify(bars, daily_vp=daily_vp)
        return RegimeView(
            type=dt.type, confidence=dt.confidence,
            max_legs=dt.max_legs, grid_mode=dt.grid_mode,
            reason=dt.reason,
        )
    except Exception:
        return _NULL


def is_blocked_by_regime(
    regime: RegimeView, side: Literal["long", "short"], confidence_floor: float = 0.75
) -> tuple[bool, str]:
    """Block entries that fight a confirmed trend.

    Returns (blocked, reason). blocked=False means allow.
    """
    if regime.confidence < confidence_floor:
        return False, ""
    if regime.type == "trend_up" and side == "short":
        return True, f"regime-blocked: trend_up (conf={regime.confidence:.2f}) opposes short"
    if regime.type == "trend_down" and side == "long":
        return True, f"regime-blocked: trend_down (conf={regime.confidence:.2f}) opposes long"
    return False, ""


def grid_shape_for_regime(regime: RegimeView, side: Literal["long", "short"]) -> dict:
    """Return grid placement parameters appropriate for the current regime + side."""
    # Defaults — range / no constraint
    out = {
        "n_legs": 5,
        "step_mult": 0.5,
        "mode": "mean_reversion",
        "tighter_spacing": False,
        "safety_sl_atr_mult": 5.0,
    }
    if regime.type in ("trend_up", "trend_down") and regime.confidence >= 0.60:
        with_trend = (regime.type == "trend_up" and side == "long") or \
                     (regime.type == "trend_down" and side == "short")
        if with_trend:
            out.update({
                "n_legs": 3,
                "step_mult": 0.35,
                "mode": "directional",
                "tighter_spacing": True,
                "safety_sl_atr_mult": 3.0,
            })
    elif regime.type == "uncertain":
        out.update({
            "n_legs": 2,
            "step_mult": 0.5,
            "mode": "cautious",
            "tighter_spacing": False,
            "safety_sl_atr_mult": 7.0,
        })
    return out
