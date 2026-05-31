"""Republic — democracy's vote, but with a constitution (a real hard stop).

Same weighted-vote direction engine as `democracy` (it subclasses it), so the
*signal* is identical. The only difference is execution policy: instead of the
far-out disaster floor (~5×ATR, which fired 0× in 108 trades and crushes RR by
making the R-denominator ~4× the TP distance), Republic clamps the grid's
safety_sl to a tight structural distance — `sl_atr_mult × ATR_15m` from the
anchor (default 1.5×ATR).

This is the deliberate A/B against democracy:
  - tighter SL  → smaller R-denominator → higher realized RR per win
  - tighter SL  → the stop actually gets hit → lower win-rate, real losses

Run both side by side; compare data/strategies/{democracy,republic}/ to see the
WR-vs-RR tradeoff on the same live signal population.
"""

from __future__ import annotations

import logging

from pipeline.types import Bar

from dataclasses import replace

from .democracy import Democracy

LOG = logging.getLogger(__name__)


class Republic(Democracy):
    name = "republic"

    def settings_override(self, settings: dict) -> dict:
        """Enable the hard-SL exit (so the tighter SL actually closes cycles) and
        disable Claude hedge-eval (keep the paper A/B deterministic + free)."""
        cyc = {**(settings.get("cycle") or {}),
               "hard_sl_exit": True, "hedge_eval_enabled": False}
        return {**settings, "cycle": cyc}

    def adjust_plan(self, plan, bar: Bar, settings: dict):
        sl_atr_mult = float(self.config.get("sl_atr_mult", 1.5))
        atr15 = self._atr15(bar.symbol)
        if atr15 <= 0:
            LOG.warning(f"[republic] {bar.symbol} no ATR — keeping disaster SL")
            return plan

        anchor = plan.anchor_price
        dist = sl_atr_mult * atr15
        new_sl = anchor - dist if plan.side == "long" else anchor + dist

        # Only tighten — never loosen past the disaster floor. If the plan has no
        # safety_sl (None), the tightened SL is unconditionally the stop.
        if plan.safety_sl is None:
            pass
        elif plan.side == "long":
            new_sl = max(new_sl, plan.safety_sl)
        else:
            new_sl = min(new_sl, plan.safety_sl)

        _prev = f"{plan.safety_sl:.2f}" if plan.safety_sl is not None else "none"
        LOG.info(
            f"[republic] {bar.symbol} {plan.side} SL tightened "
            f"{_prev} → {new_sl:.2f} ({sl_atr_mult}×ATR={dist:.2f})"
        )
        # GridPlan is frozen — return a copy with the tightened SL (+ its offset_pct
        # kept consistent for live venue translation).
        new_offset = (new_sl - anchor) / anchor if anchor > 0 else plan.safety_sl_offset_pct
        return replace(plan, safety_sl=new_sl, safety_sl_offset_pct=new_offset)

    @staticmethod
    def _atr15(symbol: str) -> float:
        try:
            from pipeline.features.atr import atr_from_store
            a = atr_from_store(symbol, "15m", period=14)
            if a <= 0:
                a = atr_from_store(symbol, "1m", period=14) * 15
            return a or 0.0
        except Exception:
            return 0.0
