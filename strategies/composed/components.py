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
    # ── limit-entry signals (e.g. choch fib retrace) ──────────────────────────
    # kind="market": immediate; entry via entry component, sl/tp via sl/tp comps.
    # kind="limit": arm a resting limit at entry_level; engine waits ≤expiry_bars
    #   for a touch then fills. sl/tp are PRECOMPUTED on the signal (coupled to the
    #   fib leg), so the engine uses them directly (skips sl/tp components).
    kind: str = "market"
    entry_level: float | None = None
    sl: float | None = None
    tp: float | None = None
    expiry_bars: int = 0


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


@trigger("choch_fib")
def _choch_fib(params: dict):
    """Structure-flip (ChoCh) → Fib retrace LIMIT. Faithful port of
    ReversalChoch._arm_signal: detect a fresh 15m ChoCh, build the impulse-leg Fib
    entry/SL/TP, arm a limit (kind="limit"). seen_choch dedup lives in ctx.state."""
    swing_n = int(params.get("swing_n", 2))
    lookback = int(params.get("choch_lookback", 200))
    arm_within = int(params.get("arm_within", 10))
    fib_entry = float(params.get("fib_entry", 0.618))
    fib_ext = float(params.get("fib_ext", 1.618))
    expiry_bars = int(params.get("entry_expiry_bars", 6))
    sl_buf_atr = float(params.get("sl_buf_atr", 0.10))
    min_sl_mult = float(params.get("min_sl_atr_mult", 0.5))

    def run(ctx: Ctx) -> Signal | None:
        from pipeline.features.choch import detect_choch, impulse_leg
        from pipeline.features.atr import atr
        from pipeline.state_store import store
        decide_tf = str(ctx.config.get("decide_tf") or "15m")
        need = lookback + 10
        bars = store().recent(ctx.symbol, decide_tf, need)
        if len(bars) < 2 * swing_n + 6:
            return None
        seen = ctx.state.setdefault("seen_choch", {})
        win = bars[-lookback:] if len(bars) > lookback else bars
        event = detect_choch(win, n=swing_n, lookback_bars=lookback)
        if event is None:
            return None
        if seen.get(ctx.symbol) == event.broken_at_ts:
            return None
        leg = impulse_leg(win, event, n=swing_n)
        if leg is None:
            return None
        origin, extreme, brk_idx = leg
        if brk_idx < len(win) - arm_within:
            seen[ctx.symbol] = event.broken_at_ts
            return None
        side = "long" if event.direction == "bull" else "short"
        span = (extreme - origin) if side == "long" else (origin - extreme)
        if span <= 0:
            return None
        if side == "long":
            entry = extreme - fib_entry * span
            sl_raw = origin
            tp = extreme + (fib_ext - 1.0) * span
        else:
            entry = extreme + fib_entry * span
            sl_raw = origin
            tp = extreme - (fib_ext - 1.0) * span
        last = bars[-1]
        if (side == "long" and entry >= last.ohlc.c) or (side == "short" and entry <= last.ohlc.c):
            seen[ctx.symbol] = event.broken_at_ts
            return None
        a = atr(bars) or 0.0
        buf = sl_buf_atr * a
        min_dist = max(buf, min_sl_mult * a, 1e-9)
        if side == "long":
            sl = min(sl_raw - buf, entry - min_dist)
        else:
            sl = max(sl_raw + buf, entry + min_dist)
        if (side == "long" and not (sl < entry < tp)) or (side == "short" and not (tp < entry < sl)):
            seen[ctx.symbol] = event.broken_at_ts
            return None
        seen[ctx.symbol] = event.broken_at_ts

        entry, sl, tp = round(entry, 4), round(sl, 4), round(tp, 4)
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        name = ctx.config.get("name", "reversal_choch")
        _ev = event

        def rationale(e, s, t, _ev=_ev, _name=name, _o=round(origin, 4), _x=round(extreme, 4)):
            return (
                f"{_name}: {side} after a 15m {_ev.direction} ChoCh "
                f"(broke {_ev.broken_level:.2f}, prior trend {_ev.last_trend}). "
                f"Impulse leg {_o:.2f}→{_x:.2f}; entry at "
                f"{fib_entry} retrace ({e:.2f}), SL beyond origin {s:.2f}, "
                f"TP at {fib_ext} extension {t:.2f}."
            )

        return Signal(side=side, entry_ref=entry, sl_raw=sl, atr=a,
                      dedup_key=event.broken_at_ts,
                      confidence=_clamp(0.45 + 0.05 * (rr - 1.0), 0.0, 0.9),
                      bias_strength=3, rationale_fn=rationale,
                      invalidation_note="price closes past the swing origin (leg invalidated) → structural SL",
                      kind="limit", entry_level=entry, sl=sl, tp=tp, expiry_bars=expiry_bars)
    return run


@trigger("wave_fib")
def _wave_fib(params: dict):
    """Two-wave (HH+HL / LL+LH) continuation → 3rd-wave LIMIT. Faithful port of
    WaveFib._arm_signal: continuation_leg structure, VP/fib pullback entry, measured-
    move TP, SL beyond the HL/LH pivot. Limit signal; seen dedup in ctx.state."""
    swing_n = int(params.get("swing_n", 2))
    struct_lookback = int(params.get("struct_lookback", 200))
    arm_within = int(params.get("arm_within", 8))
    entry_mode = str(params.get("entry_mode", "vp"))
    vp_level = str(params.get("vp_level", "poc"))
    fib_entry = float(params.get("fib_entry", 0.5))
    fib_ext = float(params.get("fib_ext", 1.0))
    expiry_bars = int(params.get("entry_expiry_bars", 6))
    sl_buf_atr = float(params.get("sl_buf_atr", 0.10))
    min_sl_mult = float(params.get("min_sl_atr_mult", 0.5))

    def run(ctx: Ctx) -> Signal | None:
        from pipeline.features.choch import continuation_leg
        from pipeline.features.atr import atr
        from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
        from pipeline.state_store import store
        decide_tf = str(ctx.config.get("decide_tf") or "15m")
        bars = store().recent(ctx.symbol, decide_tf, struct_lookback + 10)
        if len(bars) < 2 * swing_n + 6:
            return None
        win = bars[-struct_lookback:] if len(bars) > struct_lookback else bars
        st = continuation_leg(win, n=swing_n)
        if st is None:
            return None
        seen = ctx.state.setdefault("seen_wave", {})
        key = win[st.pullback_idx].close_ts
        if seen.get(ctx.symbol) == key:
            return None
        if st.pullback_idx < len(win) - arm_within:
            seen[ctx.symbol] = key
            return None
        seen[ctx.symbol] = key
        side = st.side
        span = (st.impulse - st.origin) if side == "long" else (st.origin - st.impulse)
        if span <= 0:
            return None
        last = win[-1]
        lo, hi = (st.pullback, st.impulse) if side == "long" else (st.impulse, st.pullback)
        entry = None
        entry_kind = ""
        if entry_mode == "vp":
            seg = win[st.origin_idx:st.pullback_idx + 1]
            if len(seg) >= 3:
                vp = vp_compute(seg, "intraday", win[-1].ohlc.c, bin_size=DEFAULT_BIN_SIZE.get(ctx.symbol))
                lvl = (vp.val if side == "long" else vp.vah) if vp_level == "value" else vp.poc
                entry = float(lvl) if lvl is not None else None
            entry_kind = f"vp_{vp_level}"
        if entry is None or not (lo < entry < hi):
            entry = (st.impulse - fib_entry * span) if side == "long" else (st.impulse + fib_entry * span)
            entry_kind = f"fib_{fib_entry}"
        if not (lo < entry < hi):
            entry = (lo + hi) / 2
            entry_kind = "mid"
        if (side == "long" and entry >= last.ohlc.c) or (side == "short" and entry <= last.ohlc.c):
            return None
        a = atr(bars) or 0.0
        buf = sl_buf_atr * a
        min_dist = max(buf, min_sl_mult * a, 1e-9)
        if side == "long":
            sl = min(st.pullback - buf, entry - min_dist)
            tp = st.pullback + fib_ext * span
        else:
            sl = max(st.pullback + buf, entry + min_dist)
            tp = st.pullback - fib_ext * span
        if (side == "long" and not (sl < entry < tp)) or (side == "short" and not (tp < entry < sl)):
            return None
        entry, sl, tp = round(entry, 4), round(sl, 4), round(tp, 4)
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        name = ctx.config.get("name", "wave_fib")
        _o, _i, _p, _ek = round(st.origin, 4), round(st.impulse, 4), round(st.pullback, 4), entry_kind

        def rationale(e, s, t, _o=_o, _i=_i, _p=_p, _ek=_ek, _name=name, _side=side):
            return (
                f"{_name}: {_side} continuation — confirmed two-wave structure "
                f"(wave-1 {_o:.2f}→{_i:.2f}, pullback pivot {_p:.2f}); 3rd-wave "
                f"entry[{_ek}] {e:.2f}, SL beyond the {'HL' if _side == 'long' else 'LH'} {s:.2f}, "
                f"TP {fib_ext}× measured move {t:.2f}."
            )

        return Signal(side=side, entry_ref=entry, sl_raw=sl, atr=a, dedup_key=key,
                      confidence=_clamp(0.45 + 0.05 * (rr - 1.0), 0.0, 0.9), bias_strength=3,
                      rationale_fn=rationale,
                      invalidation_note="price breaks the pullback pivot (HL/LH) → structure invalid → SL",
                      kind="limit", entry_level=entry, sl=sl, tp=tp, expiry_bars=expiry_bars)
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
