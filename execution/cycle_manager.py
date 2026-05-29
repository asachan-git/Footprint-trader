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

# Catastrophic-trend escape tracker
_trend_escape_state: dict[str, dict] = {}   # cycle_id -> {bars_against, last_close, prev_low, prev_high}


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
            rationale=f"pending limit leg{po.leg_idx} filled @ {po.limit_price:.2f}",
            grid_leg=po.leg_idx,
            add_to_existing=True,
        )
        pos_store.add_leg(po.position_id, d, latest.bar_id, lots=float(po.lots or 0.0))
        ps.mark_filled(po.pending_id)
        filled.append({"position_id": po.position_id, "leg": po.leg_idx, "price": po.limit_price})
        LOG.info(f"[cycle] fill leg{po.leg_idx} {po.symbol} {po.side} @ {po.limit_price:.2f}")
        # Shrink TP toward avg (book profit at consistent interval on adverse fills)
        try:
            updated_pos = next((p for p in pos_store.open_positions(po.symbol) if p.position_id == po.position_id), None)
            if updated_pos:
                from execution.zone_collector import nearest_strong_zone_toward
                from pipeline.state_store import store as _store
                htf_bars = _store().recent(po.symbol, "15m", 200)
                old_tp = updated_pos.take_profit
                new_zone = nearest_strong_zone_toward(
                    po.symbol, current_tp=old_tp, avg_entry=updated_pos.avg_entry,
                    side=po.side, htf_bars=htf_bars,
                )
                if new_zone is not None:
                    pos_store.adjust_tp(po.position_id, new_zone.price,
                                        f"shrink leg{po.leg_idx}_fill avg={updated_pos.avg_entry:.2f} "
                                        f"src={new_zone.source}")
                    LOG.info(f"[cycle] TP shrunk {po.position_id} {old_tp:.2f} → "
                             f"{new_zone.price:.2f} (toward avg {updated_pos.avg_entry:.2f}, "
                             f"src={new_zone.source})")
        except Exception as _e:
            LOG.debug(f"[cycle] TP shrink skipped: {_e}")
    return filled


def _check_cycle_tp(symbol: str, latest: Bar) -> list[dict]:
    """Close any cycle whose TP has been reached AND avg_entry is in profit.

    Guards:
      - TP must be on profit side of avg (else skip — partly-filled grid)
      - SWEEP CONTINUATION: if latest bar shows sweep-continuation in cycle
        direction, skip TP close on this bar to let momentum run.
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    closed = []
    pos_store = position_store()
    for pos in pos_store.open_positions(symbol):
        tp = pos.take_profit
        if pos.side == "long" and tp <= pos.avg_entry:
            continue
        if pos.side == "short" and tp >= pos.avg_entry:
            continue
        # Sweep-continuation override — let TP run if a fresh sweep_reclaim aligns with cycle
        if _sweep_continuation_active(symbol, pos.side):
            LOG.info(f"[cycle] sweep_reclaim on {symbol} {pos.side} — TP held")
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
            LOG.info(f"[cycle] TP HIT {pos.position_id} {pos.side} @ {tp:.2f} R={realized_r:.2f}")
    return closed


def _sweep_continuation_active(symbol: str, side: Literal["long", "short"]) -> bool:
    """True if there is a fresh sweep_reclaim event that aligns with cycle direction.

    sweep_reclaim on sweep_low + long cycle = downside liquidity grabbed, bulls reclaimed
    sweep_reclaim on sweep_high + short cycle = upside liquidity grabbed, bears reclaimed
    """
    try:
        from pipeline.features.sweep import active_sweeps
        evs = active_sweeps(symbol)
        for ev in evs:
            if ev.pattern != "sweep_reclaim" or ev.age_bars > 5:
                continue
            if side == "long" and ev.sweep_type == "sweep_low":
                return True
            if side == "short" and ev.sweep_type == "sweep_high":
                return True
    except Exception:
        pass
    return False


def _trail_sl_on_favorable_move(symbol: str, latest: Bar) -> list[dict]:
    """For each open cycle, if price has moved favorably without filling more legs,
    trail trail_sl to the next strong zone between current_sl and current price.
    Sweep-continuation override: skip TP check on the bar (but caller handles that);
    here we just keep advancing SL aggressively.
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    from execution.zone_collector import nearest_strong_zone_beyond
    from pipeline.state_store import store as _store

    trailed = []
    pos_store = position_store()
    htf_bars = _store().recent(symbol, "15m", 200)

    for pos in pos_store.open_positions(symbol):
        cur_price = latest.ohlc.c
        old_sl = pos.stop_loss
        # Only trail if cycle is currently profitable (price in favor of avg)
        in_profit = ((pos.side == "long" and cur_price > pos.avg_entry) or
                     (pos.side == "short" and cur_price < pos.avg_entry))
        if not in_profit:
            continue

        zone = nearest_strong_zone_beyond(
            symbol, current_price=cur_price, current_sl=old_sl,
            side=pos.side, htf_bars=htf_bars,
        )
        if zone is None:
            continue

        # Trail monotonically — only tighter
        better = ((pos.side == "long" and zone.price > old_sl) or
                  (pos.side == "short" and zone.price < old_sl))
        if not better:
            continue

        pos_store.adjust_sl(
            pos.position_id, zone.price,
            f"trail {pos.side} → {zone.source} @ {zone.price:.2f} (was {old_sl:.2f}, price {cur_price:.2f})",
        )
        trailed.append({"position_id": pos.position_id, "old_sl": old_sl, "new_sl": zone.price,
                        "source": zone.source})
        LOG.info(f"[cycle] SL trail {pos.position_id} {pos.side} {old_sl:.2f} → "
                 f"{zone.price:.2f} ({zone.source})")
    return trailed


def _check_catastrophic_trend_escape(symbol: str, latest: Bar, settings: dict | None = None) -> list[dict]:
    """Force-close cycles when price runs K consecutive bars against avg
    WITHOUT a bounce (no leg fill, no favorable reversal close) AND moved
    ≥ N×ATR adverse. Protects against runaway trends misclassified as range.
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    from pipeline.features.atr import atr_from_store

    cfg = (settings or {}).get("cycle", {}).get("trend_escape", {})
    K_bars = int(cfg.get("bars", 3))
    N_atr = float(cfg.get("atr_mult", 1.5))

    atr_15 = atr_from_store(symbol, "15m", period=14) or 0.0
    if atr_15 <= 0:
        atr_15 = max(latest.ohlc.h - latest.ohlc.l, 1e-6) * 4

    closed = []
    pos_store = position_store()
    for pos in pos_store.open_positions(symbol):
        st = _trend_escape_state.setdefault(
            pos.position_id,
            {"bars_against": 0, "last_close": latest.ohlc.c,
             "fills_seen_at_bar": -1, "last_bar_id": ""},
        )
        # Skip if same bar already processed (idempotency)
        if st["last_bar_id"] == latest.bar_id:
            continue
        st["last_bar_id"] = latest.bar_id

        # Determine "against" direction
        c = latest.ohlc.c
        avg = pos.avg_entry
        if pos.side == "long":
            against_this_bar = c < st["last_close"]   # closed lower than prev
            adverse_distance = avg - c
        else:
            against_this_bar = c > st["last_close"]   # closed higher than prev
            adverse_distance = c - avg
        st["last_close"] = c

        # Check if a leg filled this bar (any bounce/fill resets streak)
        try:
            filled_now = pending_store().open_for_position(pos.position_id)
            # The count of OPEN pending dropped from previous tick = leg filled
            curr_pending = len(filled_now)
            if st.get("prev_pending_count", curr_pending) > curr_pending:
                st["bars_against"] = 0   # leg fill = bounce = reset
            st["prev_pending_count"] = curr_pending
        except Exception:
            pass

        if against_this_bar:
            st["bars_against"] += 1
        else:
            st["bars_against"] = 0

        if st["bars_against"] >= K_bars and adverse_distance >= N_atr * atr_15:
            risk = abs(pos.avg_entry - pos.stop_loss) or 1e-9
            realized_r = -adverse_distance / risk
            reason = (f"trend-escape: {st['bars_against']} bars against, "
                      f"{adverse_distance:.2f}pts ≥ {N_atr:.1f}×ATR ({N_atr * atr_15:.2f})")
            if pos_store.close_position(pos.position_id, reason=reason, realized_r=realized_r):
                pending_store().cancel_for_position(pos.position_id)
                _va_state.pop(pos.position_id, None)
                _trend_escape_state.pop(pos.position_id, None)
                closed.append({"position_id": pos.position_id, "realized_r": round(realized_r, 3)})
                LOG.warning(f"[cycle][trend-escape] {pos.position_id} {pos.side} FORCE-CLOSED — {reason}")
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
                LOG.info(f"[cycle][VA] {pos.position_id} BROKE level={level:.2f} close={c:.2f}")
        elif st.phase == "BROKE":
            # Retest = price returns to within 0.1% of level
            tol = abs(level) * 0.001
            if abs(c - level) <= tol or (side == "long" and latest.ohlc.h >= level) or \
               (side == "short" and latest.ohlc.l <= level):
                st.phase = "RETESTING"
                st.retested_at_ts = latest.close_ts
                LOG.info(f"[cycle][VA] {pos.position_id} RETESTING level={level:.2f}")
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


def _cvd_confirms_invalidation(bars: list, pos_side: str, n_bars: int = 20) -> bool:
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


def _check_choch_invalidation(symbol: str, primary_tf: str = "15m") -> list[str]:
    """Check open cycles for a ChoCh against their direction.

    Requires ChoCh PLUS CVD confirmation to reduce false invalidations.
    """
    from pipeline.state_store import store
    from pipeline.features.choch import detect_choch
    from execution.position_store import position_store

    bars = store().recent(symbol, primary_tf, 200)
    if len(bars) < 20:
        return []
    ev = detect_choch(bars, n=2)
    if ev is None:
        return []

    invalidated = []
    for pos in position_store().open_positions(symbol):
        direction_match = (pos.side == "long" and ev.direction == "bear") or \
                          (pos.side == "short" and ev.direction == "bull")
        if not direction_match:
            continue
        cvd_ok = _cvd_confirms_invalidation(bars, pos.side, n_bars=20)
        if not cvd_ok:
            LOG.info(
                f"[cycle][ChoCh] {pos.position_id} ChoCh fired but no CVD confirmation — held"
            )
            continue
        invalidated.append(pos.position_id)
        LOG.warning(
            f"[cycle][ChoCh] {pos.position_id} {pos.side} INVALIDATED "
            f"ChoCh@{ev.broken_level:.2f} cvd={cvd_ok}"
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

    # TP participation adjustment — extend/shrink TP based on live order-flow score
    try:
        from pipeline.features.tp_participation import adjust_tp as _tp_adjust
        from pipeline.state_store import store as _store
        from pipeline.features.vp_cache import get as _vp_get
        from pipeline.features.atr import atr as _atr
        _recent = _store().recent(symbol, primary_tf, 25)
        _daily_vp = _vp_get(symbol, "daily")
        _atr_val = _atr(_recent) if _recent else None
        from execution.position_store import position_store as _pos_store
        _pstore = _pos_store()
        for _pos in _pstore.open_positions(symbol):
            if _pos.tp is None or _atr_val is None:
                continue
            _new_tp, _reason = _tp_adjust(
                position_id=_pos.position_id,
                side=_pos.side,
                bars=_recent,
                current_tp=_pos.tp,
                avg_entry=_pos.avg_entry,
                atr_val=_atr_val,
                daily_vp=_daily_vp,
            )
            if _new_tp != _pos.tp:
                _pstore.adjust_tp(_pos.position_id, _new_tp, "tp_participation")
                LOG.info(f"[cycle][tp_part] {symbol} pos={_pos.position_id} tp {_pos.tp:.2f}→{_new_tp:.2f} ({_reason})")
    except Exception as e:
        LOG.debug(f"[cycle] tp_participation failed: {e}")

    # SL trailing on favorable move (only profitable cycles trailed)
    try:
        actions["sl_trailed"] = _trail_sl_on_favorable_move(symbol, latest)
    except Exception as e:
        LOG.exception(f"[cycle] sl_trail failed: {e}")
        actions["sl_trail_error"] = str(e)

    # Catastrophic-trend escape — force-close cycles running away against
    try:
        actions["trend_escaped"] = _check_catastrophic_trend_escape(symbol, latest, settings)
    except Exception as e:
        LOG.exception(f"[cycle] trend_escape check failed: {e}")
        actions["trend_escape_error"] = str(e)

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
