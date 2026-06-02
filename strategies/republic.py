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

        # ── Structural SL with VP/LVN confluence ──────────────────────────────
        # Stop just beyond the entry candle's sell/buy zone (the extreme where the
        # aggressors stacked), used ONLY when that level is in confluence with a VP
        # HVN/POC/VAH/VAL or sits in an LVN gap. Validated 2026-06-02
        # (scripts/validate_sl_confluence.py, robust over buf/conf/rr): struct+
        # confluence ≈ +0.5R vs +0.17R for plain 1.5×ATR on confirmed setups. With
        # no confluence, fall back to the 1.5×ATR stop.
        buf = float(self.config.get("sl_struct_buf_atr", 0.2))
        conf_tol = float(self.config.get("sl_conf_tol_atr", 0.25))
        extreme = bar.ohlc.h if plan.side == "short" else bar.ohlc.l
        sl_struct = (extreme + buf * atr15) if plan.side == "short" else (extreme - buf * atr15)
        atr_sl = (anchor + sl_atr_mult * atr15) if plan.side == "short" else (anchor - sl_atr_mult * atr15)
        if self._sl_confluent(bar.symbol, sl_struct, atr15, conf_tol):
            new_sl, sl_src = sl_struct, "struct_confluence"
        else:
            new_sl, sl_src = atr_sl, f"{sl_atr_mult}×ATR"

        # Never loosen past the disaster floor (plan.safety_sl), when present.
        if plan.safety_sl is not None:
            new_sl = (max(new_sl, plan.safety_sl) if plan.side == "long"
                      else min(new_sl, plan.safety_sl))

        LOG.info(f"[republic] {bar.symbol} {plan.side} SL → {new_sl:.2f} ({sl_src})")
        new_offset = (new_sl - anchor) / anchor if anchor > 0 else plan.safety_sl_offset_pct

        # ── Footprint-confirmation TP scaling ─────────────────────────────────
        # obs(n=108, data/observations/absorption_obs.jsonl): strat_confirm setups
        # are +EV at *every* TP and improve as TP runs out (+0.073R@1.5×ATR →
        # +0.145R@3.5×ATR), while unconfirmed setups never pay (−0.2..−0.3R) so a
        # farther TP can't rescue them. bias_strength is the aggregate footprint
        # vote (incl. the absorption-confirmation vote) — used here as the confirm
        # proxy. On a strong vote, re-target TP to a real footprint zone ≥ N×ATR
        # out (capture the MFE); leave weak-vote TPs exactly as plan_grid set them.
        new_tp = plan.take_profit
        new_tp_source = plan.tp_source
        new_tp_offset = plan.tp_offset_pct
        conf_bias_min = int(self.config.get("tp_conf_bias_min", 4))
        conf_atr_mult = float(self.config.get("tp_conf_atr_mult", 3.0))
        if plan.bias_strength >= conf_bias_min and plan.legs:
            try:
                from execution.tp_resolver import resolve_tp
                leg1 = plan.legs[0].price
                cand = resolve_tp(bar.symbol, plan.side, anchor=leg1,
                                  min_distance=conf_atr_mult * atr15)
                # Only push TP farther — never pull it closer than plan_grid's.
                if cand is not None and (
                    (plan.side == "long" and cand.price > plan.take_profit)
                    or (plan.side == "short" and cand.price < plan.take_profit)
                ):
                    new_tp = cand.price
                    new_tp_source = f"conf_tp:{cand.source}"
                    new_tp_offset = (new_tp - anchor) / anchor if anchor > 0 else plan.tp_offset_pct
                    LOG.info(
                        f"[republic] {bar.symbol} {plan.side} TP pushed "
                        f"{plan.take_profit:.2f} → {new_tp:.2f} "
                        f"(bias={plan.bias_strength}≥{conf_bias_min}, "
                        f"{conf_atr_mult}×ATR, {new_tp_source})"
                    )
            except Exception as e:
                LOG.warning(f"[republic] {bar.symbol} TP scale failed: {e}")

        # GridPlan is frozen — return a copy with tightened SL + (maybe) pushed TP,
        # offset_pcts kept consistent for live cross-venue translation.
        return replace(
            plan,
            safety_sl=new_sl, safety_sl_offset_pct=new_offset,
            take_profit=new_tp, tp_source=new_tp_source, tp_offset_pct=new_tp_offset,
        )

    @staticmethod
    def _sl_confluent(symbol: str, sl: float, atr: float, tol_atr: float) -> bool:
        """True if the stop level lines up with a VP HVN/POC/VAH/VAL (within
        tol_atr×ATR) or sits inside an LVN gap. Uses live daily/weekly VP cache."""
        if atr <= 0:
            return False
        tol = tol_atr * atr
        try:
            from pipeline.features.vp_cache import get as vp_get
        except Exception:
            return False
        for period in ("daily", "weekly"):
            vp = vp_get(symbol, period)
            if not vp:
                continue
            for hvn in vp.get("hvn_zones") or []:
                if abs(sl - (hvn["low"] + hvn["high"]) / 2) <= tol:
                    return True
            for key in ("poc", "vah", "val", "naked_poc"):
                v = vp.get(key)
                if isinstance(v, (int, float)) and v > 0 and abs(sl - v) <= tol:
                    return True
            for lvn in vp.get("lvn_zones") or []:
                if lvn["low"] <= sl <= lvn["high"]:
                    return True
        return False

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
