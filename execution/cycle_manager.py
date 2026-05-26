"""Cycle manager — heartbeat for the mechanical grid trading loop.

Runs on every primary-TF bar close (hooked from pipeline/normalizer.py).

Responsibilities per symbol:
  1. Fill pending limit-order legs whose limit price has been crossed.
  2. Check cycle TP — close all cycle legs at the common TP.
  3. Detect cycle invalidation: ChoCh against cycle direction OR VAH/VAL
     break + retest + reject against cycle direction.
  4. On invalidation: flag for hedge-eval (calls /decide internally).
     If Claude returns opposite-direction with sufficient confidence,
     fire opposite-direction grid via grid_placer. Cycle-1 stays open.
  5. Hedge accounting: if hedge later closes at loss → cycle-1 new TP
     extended to recover hedge loss. If hedge closes at TP → use profit
     to offset cycle-1 loss.

Invalidation state machine (per cycle):
  NONE → BROKE (close beyond VAH/VAL against cycle)
       → RETESTING (price returned to the level)
       → REJECTED (closed back through against the cycle) → INVALIDATED
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from pipeline.types import Bar

LOG = logging.getLogger(__name__)


@dataclass
class _VAState:
    """Tracks VAH/VAL break+retest+reject state per cycle."""
    cycle_id: str
    level: float                     # the VA level being watched (VAL for long, VAH for short)
    cycle_side: Literal["long", "short"]
    phase: str = "NONE"              # NONE | BROKE | RETESTING | REJECTED
    broke_at_ts: int = 0
    retested_at_ts: int = 0


_va_state: dict[str, _VAState] = {}    # cycle_id -> state


def _fill_pending_legs(symbol: str, latest: Bar) -> list[dict]:
    """Fill any pending leg whose limit price was crossed by the latest bar."""
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    from llm.schema import Decision

    filled = []
    ps = pending_store()
    pos_store = position_store()
    for po in ps.open_for(symbol):
        crossed = False
        if po.side == "long" and latest.ohlc.l <= po.limit_price:
            crossed = True
        elif po.side == "short" and latest.ohlc.h >= po.limit_price:
            crossed = True
        if not crossed:
            continue
        # Synthesize a Decision for add_leg
        d = Decision(
            side=po.side, entry=po.limit_price,
            stop_loss=po.safety_sl or (po.limit_price * 0.95 if po.side == "long" else po.limit_price * 1.05),
            take_profit=po.tp,
            confidence=0.5,
            rationale=f"pending limit leg{po.leg_idx} filled @ {po.limit_price:.4f}",
            grid_leg=po.leg_idx,
            add_to_existing=True,
        )
        pos_store.add_leg(po.position_id, d, latest.bar_id)
        ps.mark_filled(po.pending_id)
        filled.append({"position_id": po.position_id, "leg": po.leg_idx, "price": po.limit_price})
        LOG.info(f"[cycle] fill leg{po.leg_idx} {po.symbol} {po.side} @ {po.limit_price:.4f}")
        # Re-resolve TP against new avg_entry so cycle target tracks market structure
        try:
            updated_pos = next((p for p in pos_store.open_positions(po.symbol) if p.position_id == po.position_id), None)
            if updated_pos:
                from execution.tp_resolver import resolve_tp
                from pipeline.state_store import store as _store
                htf_bars = _store().recent(po.symbol, "15m", 200)
                new_tp_cand = resolve_tp(po.symbol, po.side, anchor=updated_pos.avg_entry,
                                         htf_bars=htf_bars, min_distance=0.0)
                if new_tp_cand is not None:
                    # Only update if new TP is better (further from avg in profit direction)
                    old_tp = updated_pos.take_profit
                    improvement = (po.side == "long" and new_tp_cand.price > old_tp) or \
                                  (po.side == "short" and new_tp_cand.price < old_tp)
                    if improvement:
                        pos_store.adjust_tp(po.position_id, new_tp_cand.price,
                                            f"leg{po.leg_idx}_fill avg={updated_pos.avg_entry:.4f} src={new_tp_cand.source}")
                        LOG.info(f"[cycle] TP updated {po.position_id} {old_tp:.4f} → {new_tp_cand.price:.4f} ({new_tp_cand.source})")
        except Exception as _e:
            LOG.debug(f"[cycle] TP re-resolve skipped: {_e}")
    return filled


def _check_cycle_tp(symbol: str, latest: Bar) -> list[dict]:
    """Close any cycle whose TP has been reached AND would be profitable
    given the current weighted avg_entry.

    Guard: skip TP check while avg_entry is on the wrong side of TP. Otherwise
    a partly-filled short grid (avg=leg1 only, well below the cycle TP that
    assumed full fill) would close immediately at a loss.
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    closed = []
    pos_store = position_store()
    for pos in pos_store.open_positions(symbol):
        tp = pos.take_profit
        # TP must already be in the profitable direction vs current avg_entry
        if pos.side == "long" and tp <= pos.avg_entry:
            continue
        if pos.side == "short" and tp >= pos.avg_entry:
            continue
        hit = False
        if pos.side == "long" and latest.ohlc.h >= tp:
            hit = True
        elif pos.side == "short" and latest.ohlc.l <= tp:
            hit = True
        if not hit:
            continue
        # Realized R = (tp - avg_entry) / (avg_entry - safety_sl)
        risk = abs(pos.avg_entry - pos.stop_loss) or 1e-9
        if pos.side == "long":
            realized_r = (tp - pos.avg_entry) / risk
        else:
            realized_r = (pos.avg_entry - tp) / risk
        if pos_store.close_position(pos.position_id, reason="cycle_tp", realized_r=realized_r):
            pending_store().cancel_for_position(pos.position_id)
            _va_state.pop(pos.position_id, None)
            closed.append({"position_id": pos.position_id, "realized_r": round(realized_r, 3)})
            LOG.info(f"[cycle] TP HIT {pos.position_id} {pos.side} @ {tp:.4f} R={realized_r:.2f}")
    return closed


def _va_level_for_cycle(symbol: str, side: Literal["long", "short"]) -> float | None:
    """Pick the VA level that invalidates a cycle: VAL for long, VAH for short."""
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily")
        if not vp:
            return None
        return vp.get("val") if side == "long" else vp.get("vah")
    except Exception:
        return None


def _check_va_break(symbol: str, latest: Bar) -> list[str]:
    """Track VAH/VAL break+retest+reject sequence per open cycle.

    Returns list of cycle_ids that hit REJECTED state this bar (invalidation).
    """
    from execution.position_store import position_store
    invalidated = []
    for pos in position_store().open_positions(symbol):
        side = pos.side
        if side not in ("long", "short"):
            continue
        level = _va_level_for_cycle(symbol, side)
        if level is None:
            continue

        st = _va_state.setdefault(
            pos.position_id,
            _VAState(cycle_id=pos.position_id, level=level, cycle_side=side),
        )
        # Update tracked level if VA changed (new daily session)
        st.level = level
        c = latest.ohlc.c

        if st.phase == "NONE":
            # Broke = close beyond level against cycle (long: close < VAL, short: close > VAH)
            if (side == "long" and c < level) or (side == "short" and c > level):
                st.phase = "BROKE"
                st.broke_at_ts = latest.close_ts
                LOG.info(f"[cycle][VA] {pos.position_id} BROKE level={level:.4f} close={c:.4f}")
        elif st.phase == "BROKE":
            # Retest = price returns to within 0.1% of level
            tol = abs(level) * 0.001
            if abs(c - level) <= tol or (side == "long" and latest.ohlc.h >= level) or \
               (side == "short" and latest.ohlc.l <= level):
                st.phase = "RETESTING"
                st.retested_at_ts = latest.close_ts
                LOG.info(f"[cycle][VA] {pos.position_id} RETESTING level={level:.4f}")
        elif st.phase == "RETESTING":
            # Reject = close back beyond level against cycle (full structure event)
            if (side == "long" and c < level) or (side == "short" and c > level):
                st.phase = "REJECTED"
                invalidated.append(pos.position_id)
                LOG.warning(f"[cycle][VA] {pos.position_id} REJECTED — invalidation confirmed")
            elif (side == "long" and c > level * 1.002) or (side == "short" and c < level * 0.998):
                # Recovered back inside VA — reset state
                st.phase = "NONE"
                LOG.info(f"[cycle][VA] {pos.position_id} recovered, state reset")
    return invalidated


def _cvd_confirms_invalidation(bars: list, pos_side: str, n_bars: int = 3) -> bool:
    """True if last N bars show CVD moving AGAINST cycle direction.

    Bearish CVD confirmation (invalidates long cycle):
      - Most recent N bars have negative delta (net selling pressure)
    Bullish CVD confirmation (invalidates short cycle):
      - Most recent N bars have positive delta
    """
    recent = [b for b in bars[-n_bars:] if b.delta is not None]
    if len(recent) < 2:
        return False
    if pos_side == "long":
        return sum(1 for b in recent if (b.delta or 0) < 0) >= len(recent) - 1
    return sum(1 for b in recent if (b.delta or 0) > 0) >= len(recent) - 1


def _wick_trap_confirms_invalidation(bars: list, fps: list, pos_side: str) -> bool:
    """True if a wick-trap signal supports the OPPOSITE direction (confirming invalidation)."""
    try:
        from pipeline.features.wick_trap import wick_trap_signal
        trap = wick_trap_signal(bars, fps)
        if trap is None or trap.confidence < 0.40:
            return False
        # bull_trap confirms invalidation of a SHORT cycle (buyers squeezed out)
        # bear_trap confirms invalidation of a LONG cycle (buyers trapped)
        return (pos_side == "short" and trap.side == "bull_trap") or \
               (pos_side == "long" and trap.side == "bear_trap")
    except Exception:
        return False


def _check_choch_invalidation(symbol: str, primary_tf: str = "15m") -> list[str]:
    """Check open cycles for a ChoCh against their direction.

    Requires ChoCh PLUS at least one of: CVD confirmation OR wick-trap confirmation.
    Reduces false invalidations from brief structural breaks.
    """
    from pipeline.state_store import store
    from pipeline.features.choch import detect_choch
    from pipeline.footprint import build as build_fp
    from execution.position_store import position_store

    bars = store().recent(symbol, primary_tf, 200)
    if len(bars) < 20:
        return []
    ev = detect_choch(bars, n=2)
    if ev is None:
        return []

    fps = [build_fp(b) for b in bars[-5:]]

    invalidated = []
    for pos in position_store().open_positions(symbol):
        direction_match = (pos.side == "long" and ev.direction == "bear") or \
                          (pos.side == "short" and ev.direction == "bull")
        if not direction_match:
            continue
        # Require CVD or wick-trap confirmation
        cvd_ok = _cvd_confirms_invalidation(bars, pos.side, n_bars=3)
        wick_ok = _wick_trap_confirms_invalidation(bars[-5:], fps, pos.side)
        if not (cvd_ok or wick_ok):
            LOG.info(
                f"[cycle][ChoCh] {pos.position_id} ChoCh fired but no CVD/wick confirmation — held"
            )
            continue
        invalidated.append(pos.position_id)
        LOG.warning(
            f"[cycle][ChoCh] {pos.position_id} {pos.side} INVALIDATED "
            f"ChoCh@{ev.broken_level:.4f} cvd={cvd_ok} wick={wick_ok}"
        )
    return invalidated


def _trigger_hedge_eval(position_id: str, symbol: str, settings: dict) -> dict | None:
    """Cycle was invalidated by ChoCh / VA break. Ask Claude to confirm opposite direction.
    If Claude returns opposite-side with confidence ≥ threshold, fire hedge grid.

    Returns a dict describing the hedge action taken, or None if no hedge fired.
    """
    from execution.position_store import position_store
    pos = next((p for p in position_store().open_positions(symbol) if p.position_id == position_id), None)
    if pos is None:
        return None
    opposite = "short" if pos.side == "long" else "long"

    # Internal /decide call — bypass blueprint by importing the function directly
    try:
        from server.routes.decide import _has_setup  # noqa: F401
        from pipeline.state_store import store
        from llm.client import ClaudeClient, ClientConfig
        from prompts.builder import cached_prefix, variable_suffix
        import json as _json
    except Exception as e:
        LOG.warning(f"[cycle][hedge-eval] dependencies missing: {e}")
        return None

    primary_tf = str(settings.get("instrument", {}).get("primary_tf", "15m"))
    s = store()
    latest = s.latest(symbol, primary_tf)
    if latest is None:
        return None
    recent = s.recent(symbol, primary_tf, settings["prompt"]["recent_bars"])
    higher = {tf: s.as_of(symbol, tf, latest.close_ts)
              for tf in settings["instrument"]["timeframes"] if tf != primary_tf}
    suffix_dict = _json.loads(variable_suffix(recent, higher))
    suffix_dict["hedge_eval"] = {
        "current_cycle_side": pos.side,
        "current_cycle_avg_entry": pos.avg_entry,
        "reason": "cycle invalidated (ChoCh or VAH/VAL break+retest+reject)",
        "requested_direction": opposite,
        "note": "Confirm OPPOSITE direction only if structural break is strong. Return flat if uncertain.",
    }
    suffix = _json.dumps(suffix_dict)
    prefix = cached_prefix(settings["prompt"]["few_shot_count"])

    cfg = ClientConfig(
        model=settings["claude"]["model"],
        max_tokens=settings["claude"]["max_tokens_out"],
        timeout_s=settings["claude"]["timeout_s"],
    )
    try:
        client = ClaudeClient(cfg)
        decision = client.decide(prefix, suffix)
    except Exception as e:
        LOG.warning(f"[cycle][hedge-eval] Claude call failed: {e}")
        return None

    min_hedge_conf = float(((settings.get("grid") or {}).get("hedge", {})).get("min_confidence", 0.55))
    if decision.side != opposite or decision.confidence < min_hedge_conf:
        LOG.info(f"[cycle][hedge-eval] declined: side={decision.side} conf={decision.confidence:.2f} (need {opposite} ≥ {min_hedge_conf})")
        return {"hedged": False, "reason": "claude_declined", "claude_side": decision.side, "conf": decision.confidence}

    # Fire hedge grid via dispatcher with add_to_existing semantics (separate cycle)
    return _fire_hedge_grid(parent_position_id=position_id, opposite=opposite,
                            decision=decision, latest=latest, settings=settings)


def _fire_hedge_grid(parent_position_id: str, opposite: str, decision, latest: Bar, settings: dict) -> dict:
    """Place a hedge grid opposite to the invalidated cycle."""
    from execution.grid_placer import plan_grid
    from pipeline.features.atr import atr_from_store

    bs = getattr(decision, "bias_strength", 3)
    exec_cfg = settings.get("execution", {}) or {}
    broker_symbol = (exec_cfg.get("symbol_map") or {}).get(latest.symbol, latest.symbol)
    base_lot = float((exec_cfg.get("default_lots") or {}).get(broker_symbol, 0.01))
    atr15 = atr_from_store(latest.symbol, "15m", period=14) or atr_from_store(latest.symbol, "1m", period=14) * 15 or 1.0

    plan = plan_grid(
        symbol=latest.symbol, broker_symbol=broker_symbol,
        direction=opposite, anchor=latest.ohlc.c, bias_strength=int(bs),
        atr_15m=atr15, base_lot=base_lot, htf_bars=None,
    )
    # Dispatch via the same paths as fresh grids — but tag as hedge child
    from execution.router import dispatch_grid
    result = dispatch_grid(plan, latest, settings, parent_position_id=parent_position_id)
    LOG.warning(f"[cycle][hedge] FIRED opposite={opposite} parent={parent_position_id} → {result.get('position_id') or result.get('skipped')}")
    return {"hedged": True, "parent_position_id": parent_position_id, "result": result}


def on_bar_close(symbol: str, primary_tf: str, latest: Bar, settings: dict | None = None) -> dict:
    """Heartbeat — called from normalizer after each primary-TF bar close.

    Returns a summary dict of actions taken this tick.
    """
    actions: dict = {"symbol": symbol, "bar_id": latest.bar_id}
    try:
        actions["filled"] = _fill_pending_legs(symbol, latest)
    except Exception as e:
        LOG.exception(f"[cycle] fill_pending failed: {e}")
        actions["filled_error"] = str(e)
    try:
        actions["closed_tp"] = _check_cycle_tp(symbol, latest)
    except Exception as e:
        LOG.exception(f"[cycle] check_tp failed: {e}")
        actions["tp_error"] = str(e)

    invalidated: list[str] = []
    try:
        invalidated += _check_va_break(symbol, latest)
    except Exception as e:
        LOG.exception(f"[cycle] va_break check failed: {e}")
    try:
        invalidated += _check_choch_invalidation(symbol, primary_tf)
    except Exception as e:
        LOG.exception(f"[cycle] choch check failed: {e}")

    if invalidated and settings:
        hedge_results = []
        for pid in set(invalidated):
            try:
                res = _trigger_hedge_eval(pid, symbol, settings)
                if res:
                    hedge_results.append(res)
            except Exception as e:
                LOG.exception(f"[cycle] hedge_eval failed for {pid}: {e}")
        actions["hedge_evals"] = hedge_results
    return actions
