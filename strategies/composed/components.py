"""Concrete components for the composed-strategy framework.

Each component is registered (registry.py) and bound to its config params. The
exchange types:

  Ctx      — per-decide context: symbol, tf, bar, settings, config, shared state
  Signal   — a fired trigger's raw output (side, reference prices, presentation)

Lifecycle the engine runs: trigger(ctx) -> entry(signal) -> sl(signal,entry)
-> tp(signal,entry,sl) -> Decision. execution components transform the grid
plan in adjust_plan; exit components contribute settings.cycle.* flags.

This first batch covers what `coup` (trigger_mode=climax_flip) needs, mirroring
strategies/coup.py arithmetic exactly for bit-for-bit parity.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

from pipeline.types import Bar

from .registry import trigger, entry, sl_rule, tp_rule, exit_rule, execution


@dataclass
class Ctx:
    symbol: str
    tf: str
    bar: Bar
    settings: dict
    config: dict
    state: dict   # shared per-strategy-instance state (acted dedup, pending sl/tp)


@dataclass
class Signal:
    side: str                 # "long" | "short"
    entry_ref: float          # reference fill price (e.g. ignition bar close)
    sl_raw: float             # structural stop from the trigger, pre-floor
    atr: float
    dedup_key: int            # trigger ts — engine sets `acted` to this on success
    confidence: float
    bias_strength: int
    rationale_fn: Callable[[float, float, float], str]   # (entry, sl, tp) -> str
    invalidation_note: str = ""
    meta: dict = field(default_factory=dict)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ── triggers ────────────────────────────────────────────────────────────────
@trigger("climax_flip")
def _climax_flip(params: dict):
    vol_mult = float(params.get("vol_mult", 2.0))
    delta_swing = float(params.get("delta_swing", 50.0))
    vp_filter = bool(params.get("vp_filter", True))

    def run(ctx: Ctx) -> Signal | None:
        from pipeline.features.reversal_pattern import detect as _rdetect, VP_WIN
        from pipeline.features.atr import atr
        from pipeline.state_store import store
        decide_tf = str(ctx.config.get("decide_tf") or "15m")
        need = (VP_WIN.get(decide_tf, 96) if vp_filter else 30) + 30
        bars = store().recent(ctx.symbol, decide_tf, need)
        if len(bars) < 30:
            return None
        markers = _rdetect(bars, vol_mult=vol_mult, delta_swing=delta_swing,
                           symbol=ctx.symbol, tf=decide_tf, vp_filter=vp_filter,
                           entry_mode="market")
        if not markers:
            return None
        m = markers[-1]
        if m["ts"] != bars[-1].close_ts or ctx.state.get("acted", {}).get(ctx.symbol) == m["ts"]:
            return None
        side = m["side"]
        entry_ref = float(bars[-1].ohlc.c)
        atr_val = atr(bars) or 0.0
        conf = _clamp(0.45 + (m["vol_ratio"] - vol_mult) * 0.05, 0.0, 0.9)
        name = ctx.config.get("name", "coup")

        def rationale(entry, sl, tp, _m=m, _name=name):
            return (
                f"{_name}: {side} climax-flip — pivot vol×{_m['vol_ratio']} at "
                f"{_m['near_level']} ({_m['vp_pos']}), next bar flipped delta "
                f"(Δswing {_m['delta_swing']}) + closed the reversal way; ignition-bar "
                f"entry. SL[{_m['sl_basis']}] {sl:.2f}, 2R TP {tp:.2f}."
            )

        return Signal(side=side, entry_ref=entry_ref, sl_raw=float(m["sl"]), atr=atr_val,
                      dedup_key=m["ts"], confidence=conf, bias_strength=3,
                      rationale_fn=rationale,
                      invalidation_note="structural SL (imbalance/swing) hit, or winner-side flip",
                      meta={"m": m, "vol_mult": vol_mult})
    return run


# ── entry ───────────────────────────────────────────────────────────────────
@entry("market")
def _entry_market(params: dict):
    def run(signal: Signal, ctx: Ctx) -> float:
        return signal.entry_ref
    return run


# ── sl ──────────────────────────────────────────────────────────────────────
@sl_rule("atr_floor")
def _sl_atr_floor(params: dict):
    """Take the trigger's structural sl_raw, clamped to AT LEAST min_sl_atr_mult×ATR
    from entry (never a degenerate near-zero-risk stop). Mirrors Coup.decide()."""
    mult = float(params.get("min_sl_atr_mult", 0.5))

    def run(signal: Signal, entry: float, ctx: Ctx) -> float:
        min_dist = max(mult * signal.atr, 1e-9)
        if signal.side == "long":
            return min(signal.sl_raw, entry - min_dist)
        return max(signal.sl_raw, entry + min_dist)
    return run


# ── tp ──────────────────────────────────────────────────────────────────────
@tp_rule("rr")
def _tp_rr(params: dict):
    rr = float(params.get("rr", 2.0))

    def run(signal: Signal, entry: float, sl: float, ctx: Ctx) -> float | None:
        risk = (entry - sl) if signal.side == "long" else (sl - entry)
        if risk <= 0:
            return None
        return entry + rr * risk if signal.side == "long" else entry - rr * risk
    return run


# ── execution (adjust_plan transform) ─────────────────────────────────────────
@execution("single_leg")
def _exec_single_leg(params: dict):
    """Drop the grid to a single tactical leg; clamp safety SL + force TP from the
    pending values stashed by the engine in decide(). Mirrors Coup.adjust_plan."""
    tp_source = str(params.get("tp_source", "coup_2R"))

    def run(plan, bar: Bar, ctx: Ctx):
        if not plan.legs:
            return plan
        leg1 = plan.legs[0]
        anchor = plan.anchor_price or leg1.price
        offs = plan.leg_offsets_pct[:1] if plan.leg_offsets_pct else ()
        new = replace(plan, legs=[leg1], avg_entry_on_full_fill=leg1.price, leg_offsets_pct=offs)
        pend = ctx.state.get("pending", {}).get(bar.symbol, {})
        sl = pend.get("sl")
        tp = pend.get("tp")
        if sl is not None and anchor > 0:
            new = replace(new, safety_sl=sl, safety_sl_offset_pct=(sl - anchor) / anchor)
        if tp is not None and anchor > 0:
            new = replace(new, take_profit=tp, tp_source=tp_source, tp_offset_pct=(tp - anchor) / anchor)
        return new

    def policy(_params=params):
        # single-leg tactical entry → no Claude hedge-eval (matches coup)
        return {"hedge_eval_enabled": False}
    run.policy = policy
    return run


# ── exits (settings.cycle.* flag contributions) ──────────────────────────────
@exit_rule("hard_sl")
def _exit_hard_sl(params: dict):
    def flags(ctx: Ctx) -> dict:
        return {"hard_sl_exit": True}
    return flags


@exit_rule("cvd_divergence")
def _exit_cvd_div(params: dict):
    conf = float(params.get("conf", 0.65))

    def flags(ctx: Ctx) -> dict:
        return {"cvd_divergence_exit": True, "cvd_exit_conf": conf}
    return flags


@exit_rule("absorption_flip")
def _exit_flip(params: dict):
    def flags(ctx: Ctx) -> dict:
        return {"coup_flip_exit": True}
    return flags
