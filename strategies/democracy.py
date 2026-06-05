"""Democracy — the first strategy.

Named for how it decides: a panel of independent footprint/VP detectors each cast
a weighted *ballot* (long / short / abstain), and the weighted majority wins. This
wraps the existing Mode-2 rules engine (execution/direction_engine.decide_direction),
which already implements exactly that voting model.

Entry synthesis mirrors the legacy /grid_tick path: the vote only yields a side +
conviction (bias_strength); the concrete entry/SL/TP and the 5-leg grid are built
downstream by the grid placer. SL/TP here are placeholders — the mechanical grid
and cycle_manager (disaster floor, VP-anchored TP, ChoCh) own the real exits.
"""

from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from execution.direction_engine import decide_direction

from .base import Strategy

LOG = logging.getLogger(__name__)


class Democracy(Strategy):
    name = "democracy"

    def settings_override(self, settings: dict) -> dict:
        """Hold cycle TP while a high-CVD candle favors continuation in the cycle
        direction — lets winners run past the tiny mechanical TP instead of banking
        sub-1R. Addresses democracy's high-WR / negative-R skew: wins were capping
        at ~0.2R while disaster-floor losses took ~0.85R. Tunable via
        cfg.cvd_hold_conf / cfg.cvd_hold_delta_mult."""
        cyc = {
            **(settings.get("cycle") or {}),
            "cvd_continuation_hold": bool(self.config.get("cvd_continuation_hold", True)),
            "cvd_hold_conf": float(self.config.get("cvd_hold_conf", 0.70)),
            "cvd_hold_delta_mult": float(self.config.get("cvd_hold_delta_mult", 1.3)),
        }
        return {**settings, "cycle": cyc}

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        # The direction engine votes on 15m structure regardless of the feed TF.
        vote_tf = str(self.config.get("vote_tf") or "15m")
        dd = decide_direction(symbol, vote_tf)

        if dd.side == "flat":
            LOG.info(f"[democracy] {symbol} FLAT score={dd.score:.2f} ({dd.note})")
            return None

        close = bar.ohlc.c
        # Placeholder SL/TP — overwritten by the grid placer + cycle_manager.
        sl = close * (0.95 if dd.side == "long" else 1.05)
        tp = close * (1.05 if dd.side == "long" else 0.95)
        return Decision(
            side=dd.side,
            entry=close,
            stop_loss=sl,
            take_profit=tp,
            # NOT a calibrated win-probability — bias/5 is flat vs outcome
            # (validated 2026-06-02). Exposure-scaling + hedge-gate proxy only.
            confidence=min(1.0, dd.bias_strength / 5.0),
            bias_strength=dd.bias_strength,
            rationale=(
                f"democracy: weighted vote score={dd.score:.2f} "
                f"bias={dd.bias_strength}/5 | {dd.note}"
            ),
        )
