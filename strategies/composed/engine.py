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


_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class ComposedStrategy(Strategy):
    name = "composed"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config
        self.name = cfg.get("name") or self.name
        self.state: dict = {"acted": {}, "pending": {}, "pending_entry": {}, "seen_choch": {}}
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

        # ── Phase 1: resolve a pending limit entry (fill on touch / expire) ──
        pend = self.state["pending_entry"].get(symbol)
        if pend is not None:
            if bar.close_ts > pend["expiry_ts"] or self.state["acted"].get(symbol) == pend["dedup"]:
                self.state["pending_entry"].pop(symbol, None)
            elif bar.ohlc.l <= pend["level"] <= bar.ohlc.h:        # retrace touched → fill
                self.state["pending_entry"].pop(symbol, None)
                return self._commit(symbol, pend["side"], pend["level"], pend["sl"], pend["tp"],
                                    pend["dedup"], pend["confidence"], pend["bias"],
                                    pend["rationale_fn"], pend["inval"])
            else:
                return None                                        # still waiting

        # ── Phase 2: fire the trigger ──
        sig = self._trigger(ctx)
        if sig is None:
            return None

        if sig.kind == "limit":
            tf_sec = _TF_SEC.get(str(self.config.get("decide_tf") or tf), 900)
            self.state["pending_entry"][symbol] = {
                "level": sig.entry_level, "sl": sig.sl, "tp": sig.tp, "side": sig.side,
                "expiry_ts": bar.close_ts + sig.expiry_bars * tf_sec, "dedup": sig.dedup_key,
                "confidence": sig.confidence, "bias": sig.bias_strength,
                "rationale_fn": sig.rationale_fn, "inval": sig.invalidation_note,
            }
            LOG.info(f"[{self.name}] {symbol} {sig.side.upper()} armed LIMIT @{sig.entry_level:.2f} "
                     f"≤{sig.expiry_bars} bars")
            return None

        # market entry — entry via component; sl/tp precomputed on signal or via components
        entry = self._entry(sig, ctx)
        sl = sig.sl if sig.sl is not None else self._sl(sig, entry, ctx)
        tp = sig.tp if sig.tp is not None else self._tp(sig, entry, sl, ctx)
        if tp is None:
            return None
        return self._commit(symbol, sig.side, entry, sl, tp, sig.dedup_key,
                            sig.confidence, sig.bias_strength, sig.rationale_fn, sig.invalidation_note)

    def _commit(self, symbol, side, entry, sl, tp, dedup, conf, bias, rationale_fn, inval) -> Decision:
        self.state["acted"][symbol] = dedup
        self.state["pending"][symbol] = {"sl": sl, "tp": tp}
        LOG.info(f"[{self.name}] {symbol} {side.upper()} @{entry:.2f} SL={sl:.2f} TP={tp:.2f} bias={bias}")
        return Decision(side=side, entry=entry, stop_loss=sl, take_profit=tp,
                        confidence=conf, bias_strength=bias,
                        rationale=rationale_fn(entry, sl, tp), invalidation_note=inval)

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
