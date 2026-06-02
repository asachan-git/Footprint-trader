"""Senate — Republic's engine, but the stop is anchored to footprint WALLS.

Same vote signal + TP scaling + hard-SL exit as `republic`. The only A/B variable
is the SL anchor (Republic._compute_sl): instead of the candle extreme, Senate
puts the stop just beyond the favorable footprint walls on the stop side — the
stacked sell-imbalance (short) / buy-imbalance (long) zones and the same-side
big-trade prints (the "selling zone in our favour"). A VP-HVN/POC confluent wall
is tagged A+ ("wall_vp"); with no wall it falls back to sl_atr_mult×ATR.

Validated 2026-06-02 (scripts/validate_sl_levels.py, entry = honest close):
  wall SL        ≈ +0.31R vs +0.17R ATR on strat_confirm setups (robust, 98% cov)
  wall ∧ VP      ≈ +0.39R on-anchor (best per-trade, ~63% coverage)
Runs live ALONGSIDE republic (extreme+confluence) — compare
data/strategies/{republic,senate}/ for the SL-anchor A/B on the same signal.
"""

from __future__ import annotations

import logging

from pipeline.types import Bar

from .republic import Republic

LOG = logging.getLogger(__name__)


class Senate(Republic):
    name = "senate"

    def _compute_sl(self, plan, bar: Bar, atr15: float, anchor: float) -> tuple[float, str]:
        sl_atr_mult = float(self.config.get("sl_atr_mult", 1.5))
        buf = float(self.config.get("sl_wall_buf_atr", 0.25))
        conf_tol = float(self.config.get("sl_conf_tol_atr", 0.25))
        atr_sl = (anchor + sl_atr_mult * atr15) if plan.side == "short" else (anchor - sl_atr_mult * atr15)

        walls = self._favorable_walls(bar, plan.side, anchor)
        if not walls:
            return atr_sl, "atr_fallback"
        # farthest favorable wall on the stop side = the last line of defence
        w = max(walls) if plan.side == "short" else min(walls)
        sl = (w + buf * atr15) if plan.side == "short" else (w - buf * atr15)
        src = "wall_vp" if self._sl_confluent(bar.symbol, w, atr15, conf_tol) else "wall"
        return sl, src

    @staticmethod
    def _favorable_walls(bar: Bar, side: str, anchor: float) -> list[float]:
        """Footprint walls in our favour on the stop side: stacked same-direction
        imbalances + same-aggressor big-trade prints beyond the entry anchor.
        For a short → sell-imbalance / sell big-trades above; mirror for long."""
        want = "sell" if side == "short" else "buy"
        out: list[float] = []
        try:
            from pipeline.footprint import build as build_fp
            from pipeline.features.stacked_imbalance import stacked_imbalances
            fp = build_fp(bar)
            for z in stacked_imbalances(fp, min_stack=3, ratio=3.0):
                if z.side != want:
                    continue
                edge = z.price_high if side == "short" else z.price_low
                if (side == "short" and edge >= anchor) or (side == "long" and edge <= anchor):
                    out.append(edge)
        except Exception as e:
            LOG.warning(f"[senate] {bar.symbol} stacked-imbalance read failed: {e}")
        try:
            from pipeline.features.big_trade import get_recent_events
            for e in get_recent_events(bar.symbol, bar.tf, n=20):
                if e.aggressor != want:
                    continue
                if (side == "short" and e.price >= anchor) or (side == "long" and e.price <= anchor):
                    out.append(e.price)
        except Exception as e:
            LOG.warning(f"[senate] {bar.symbol} big-trade read failed: {e}")
        return out
