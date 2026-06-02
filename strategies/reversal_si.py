"""ReversalSI — reversal pattern, stacked-imbalance IGNITION entry + footprint-managed
exit. Separate strategy (own store) running alongside the lvn-entry `reversal` for a
live A/B.

Entry: the VP-gated reversal flip must ALSO print a fresh trade-direction stacked
imbalance on the flip bar (orderflow ignition; `entry_mode=si_flip`). Market entry at
the flip close; structural SL just beyond that stack's far low/high (set by the detector).

Exit (footprint-managed):
  - TARGET = the opposite-side **LVN** (value-vacuum) in the profit direction, else the
    next unfilled **FVG**, else 2R — set as the cycle TP cap.
  - At the target / on reversal, the inherited **absorption-flip** + **CVD-divergence**
    exits cut early when order-flow turns.
  - Otherwise **trail the SL** under each new bar's trade-direction stacked imbalance
    (cycle_manager `_check_si_trail`, gated `si_trail_exit`).

PROVISIONAL — one-regime edge; runs in PAPER to accumulate out-of-sample samples.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.state_store import store

from .reversal import Reversal

LOG = logging.getLogger(__name__)


class ReversalSI(Reversal):
    name = "reversal_si"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("entry_mode", "si_flip")     # fresh SI on the flip bar
        cfg.setdefault("target_mode", "vp_lvn")     # opposite LVN → FVG → 2R
        cfg.setdefault("si_trail", True)            # trail SL under each bar's SI
        cfg.setdefault("cvd_divergence_exit", True)
        super().__init__(cfg)

    def settings_override(self, settings: dict) -> dict:
        s = super().settings_override(settings)     # coup exits + cvd_divergence
        cyc = {**(s.get("cycle") or {}),
               "si_trail_exit": bool(self.config.get("si_trail", True))}
        return {**s, "cycle": cyc}

    # ── opposite-side VP/FVG target ──────────────────────────────────────────────
    def _vp_target(self, symbol: str, side: str, entry: float, min_dist: float) -> float | None:
        """Nearest opposite-side LVN mid in the profit direction; else next unfilled
        FVG; else None (caller keeps the 2R TP)."""
        try:
            from pipeline.features.vp_cache import get as vp_get
            vp = vp_get(symbol, "daily") or {}
            lvns = vp.get("lvn_zones") or []
            mids = [ (z["low"] + z["high"]) / 2 for z in lvns ]
            if side == "long":
                cand = [m for m in mids if m >= entry + min_dist]
                if cand:
                    return min(cand)
            else:
                cand = [m for m in mids if m <= entry - min_dist]
                if cand:
                    return max(cand)
        except Exception:
            pass
        try:
            from pipeline.features.fvg import detect_fvgs, nearest_fvg_above
            bars = store().recent(symbol, str(self.config.get("decide_tf") or "15m"), 200)
            fvgs = detect_fvgs(bars)
            if side == "long":
                above = [f for f in fvgs if f.low >= entry + min_dist]
                if above:
                    return min(above, key=lambda f: f.low).low
            else:
                below = [f for f in fvgs if f.high <= entry - min_dist]
                if below:
                    return max(below, key=lambda f: f.high).high
        except Exception:
            pass
        return None

    def _build_decision(self, symbol: str, m: dict, entry: float) -> Decision:
        # Default (2R) decision from the base, then retarget to the opposite VP/FVG.
        d = super()._build_decision(symbol, m, entry)
        if str(self.config.get("target_mode", "vp_lvn")) == "vp_lvn":
            risk = abs(entry - d.stop_loss)
            tgt = self._vp_target(symbol, m["side"], entry, min_dist=risk)  # ≥1R away
            if tgt is not None and ((m["side"] == "long" and tgt > entry) or
                                    (m["side"] == "short" and tgt < entry)):
                self._pending_tp[symbol] = tgt
                LOG.info(f"[reversal_si] {symbol} {m['side'].upper()} target → {tgt:.2f} "
                         f"(opp VP-LVN/FVG; 2R was {d.take_profit:.2f})")
                d = d.model_copy(update={"take_profit": tgt})
        return d
