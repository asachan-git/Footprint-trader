"""ComposedStrategy — generic engine that runs a config wiring of components.

One class, many strategies: the YAML config names the trigger/entry/sl/tp/
execution/exits components + their params; this engine resolves and runs them,
producing the same Decision / settings_override / adjust_plan outputs the legacy
Strategy subclasses produced.

Config shape:
    name: coup
    engine: composed
    decide_tf: 15m
    trigger:   {type: climax_flip, vol_mult: 2.0, delta_swing: 50.0, vp_filter: true}
    entry:     {type: market}
    sl:        {type: atr_floor, min_sl_atr_mult: 0.5}
    tp:        {type: rr, rr: 2.0}
    execution: {type: single_leg}
    exits:     [{type: hard_sl}, {type: cvd_divergence, conf: 0.65}]
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar

from ..base import Strategy
from .components import Ctx
from .registry import (TRIGGERS, ENTRIES, SL_RULES, TP_RULES, EXITS, EXECUTION, resolve)

LOG = logging.getLogger(__name__)


class ComposedStrategy(Strategy):
    name = "composed"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config
        self.name = cfg.get("name") or self.name
        self.state: dict = {"acted": {}, "pending": {}}
        self._trigger = resolve(TRIGGERS, cfg.get("trigger"), "trigger")
        self._entry = resolve(ENTRIES, cfg.get("entry", "market"), "entry")
        self._sl = resolve(SL_RULES, cfg.get("sl"), "sl")
        self._tp = resolve(TP_RULES, cfg.get("tp"), "tp")
        self._execution = resolve(EXECUTION, cfg.get("execution"), "execution") if cfg.get("execution") else None
        self._exits = [resolve(EXITS, e, "exit") for e in (cfg.get("exits") or [])]

    # ── lifecycle ────────────────────────────────────────────────────────────
    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        ctx = Ctx(symbol=symbol, tf=tf, bar=bar, settings=settings,
                  config=self.config, state=self.state)
        sig = self._trigger(ctx)
        if sig is None:
            return None
        entry = self._entry(sig, ctx)
        sl = self._sl(sig, entry, ctx)
        tp = self._tp(sig, entry, sl, ctx)
        if tp is None:
            return None
        # success — commit dedup + stash structural SL/TP for adjust_plan
        self.state["acted"][symbol] = sig.dedup_key
        self.state["pending"][symbol] = {"sl": sl, "tp": tp}
        LOG.info(f"[{self.name}] {symbol} {sig.side.upper()} @{entry:.2f} "
                 f"SL={sl:.2f} TP={tp:.2f} bias={sig.bias_strength}")
        return Decision(
            side=sig.side, entry=entry, stop_loss=sl, take_profit=tp,
            confidence=sig.confidence, bias_strength=sig.bias_strength,
            rationale=sig.rationale_fn(entry, sl, tp),
            invalidation_note=sig.invalidation_note,
        )

    def settings_override(self, settings: dict) -> dict:
        cyc = dict(settings.get("cycle") or {})
        if self._execution is not None and hasattr(self._execution, "policy"):
            cyc.update(self._execution.policy())
        for ex in self._exits:
            cyc.update(ex(None))
        return {**settings, "cycle": cyc}

    def adjust_plan(self, plan, bar: Bar, settings: dict):
        if self._execution is None:
            return plan
        ctx = Ctx(symbol=bar.symbol, tf=bar.tf, bar=bar, settings=settings,
                  config=self.config, state=self.state)
        return self._execution(plan, bar, ctx)
