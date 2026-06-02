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
        # Progressive TP shrink on adverse fill (2026-05-30).
        # Each leg fill = confirmed adverse move. Tighten TP toward nearest
        # profit-zone so recovery exits faster. Floor shrinks as more legs fill.
        # Different from old shrinker: fires ONLY on leg fills (not every bar).
        _leg_count = pos_store.get_position(po.position_id).leg_count if hasattr(pos_store, "get_position") else 0
        try:
            _pos_now = next((p for p in pos_store.open_positions(po.symbol) if p.position_id == po.position_id), None)
            if _pos_now is not None:
                _leg_count = _pos_now.leg_count
        except Exception:
            _leg_count = 0
        if _leg_count >= 2:
            try:
                from execution.zone_collector import nearest_strong_zone_toward
                from pipeline.features.atr import atr_from_store
                from pipeline.features.vp_cache import get as _vp_get
                from pipeline.state_store import store as _pstate
                _atr = atr_from_store(po.symbol, "15m", period=14) or atr_from_store(po.symbol, "1m", period=14) * 15 or 1.0
                # Floor shrinks as more legs fill — deeper adverse = closer recovery target
                _floor_mult = {2: 1.0, 3: 0.7, 4: 0.5}.get(_leg_count, 0.3)
                _htf = _pstate().recent(po.symbol, "15m", 200)
                _pos_now2 = next((p for p in pos_store.open_positions(po.symbol) if p.position_id == po.position_id), None)
                if _pos_now2 is not None:
                    _zone = nearest_strong_zone_toward(
                        po.symbol,
                        current_tp=_pos_now2.take_profit,
                        avg_entry=_pos_now2.avg_entry,
                        side=po.side,
                        htf_bars=_htf,
                        min_strength=0.6,
                    )
                    _floor_tp = (
                        _pos_now2.avg_entry + _floor_mult * _atr if po.side == "long"
                        else _pos_now2.avg_entry - _floor_mult * _atr
                    )
                    _viable = _zone is not None and (
                        (po.side == "long" and _zone.price >= _floor_tp) or
                        (po.side == "short" and _zone.price <= _floor_tp)
                    )
                    if _viable and _zone is not None and _zone.price != _pos_now2.take_profit:
                        pos_store.adjust_tp(
                            po.position_id, _zone.price,
                            f"adverse_shrink:leg{_leg_count} floor={_floor_mult:.1f}×ATR zone={_zone.source}@{_zone.price:.2f}",
                        )
                        LOG.info(f"[cycle][adverse_shrink] {po.position_id} leg{_leg_count} "
                                 f"tp {_pos_now2.take_profit:.2f}→{_zone.price:.2f} "
                                 f"floor={_floor_mult:.1f}×ATR ({_zone.source})")
            except Exception as _e:
                LOG.debug(f"[cycle][adverse_shrink] skipped: {_e}")
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


def _check_hard_sl(symbol: str, latest: Bar) -> list[dict]:
    """Close any open cycle whose stop_loss was crossed by the latest bar.

    Opt-in (caller gates on settings.cycle.hard_sl_exit). Realized R is measured
    against the cycle's own risk denominator (avg_entry − stop_loss); a clean
    stop-out at the SL is ≈ −1R. Legs are filled BEFORE this runs, so stop_loss
    reflects the latest leg.
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    closed = []
    pos_store = position_store()
    for pos in pos_store.open_positions(symbol):
        sl = pos.stop_loss
        if not sl:
            continue
        hit = (pos.side == "long" and latest.ohlc.l <= sl) or \
              (pos.side == "short" and latest.ohlc.h >= sl)
        if not hit:
            continue
        risk = abs(pos.avg_entry - sl) or 1e-9
        adverse = (pos.avg_entry - sl) if pos.side == "long" else (sl - pos.avg_entry)
        realized_r = -abs(adverse) / risk
        if pos_store.close_position(pos.position_id, reason="hard_sl", realized_r=realized_r):
            pending_store().cancel_for_position(pos.position_id)
            _va_state.pop(pos.position_id, None)
            _trend_escape_state.pop(pos.position_id, None)
            closed.append({"position_id": pos.position_id, "realized_r": round(realized_r, 3)})
            LOG.info(f"[cycle] HARD-SL {pos.position_id} {pos.side} @ {sl:.2f} R={realized_r:.2f}")
    return closed


def _check_absorption_flip(symbol: str, latest: Bar) -> list[dict]:
    """Close any open cycle whose winner side gets absorbed at an extreme — the
    mirror of the entry trigger, now against us (the `coup` strategy's exit).

    Long position → canonical absorption side=="sell" (buyers absorbed at top).
    Short position → canonical absorption side=="buy" (sellers absorbed at low).
    Opt-in (caller gates on settings.cycle.coup_flip_exit). Realized R is marked to
    the bar close against the cycle's own risk denominator (avg_entry − stop_loss).
    """
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    from pipeline.footprint import build as _build_fp
    from pipeline.features.absorption import detect_canonical_absorption

    closed: list[dict] = []
    absorbs = detect_canonical_absorption(latest, _build_fp(latest))
    if not absorbs:
        return closed
    sides = {a.side for a in absorbs}

    pos_store = position_store()
    c = latest.ohlc.c
    for pos in pos_store.open_positions(symbol):
        flip = (pos.side == "long" and "sell" in sides) or \
               (pos.side == "short" and "buy" in sides)
        if not flip:
            continue
        risk = abs(pos.avg_entry - (pos.stop_loss or 0)) or 1e-9
        pnl = (c - pos.avg_entry) if pos.side == "long" else (pos.avg_entry - c)
        realized_r = pnl / risk
        if pos_store.close_position(pos.position_id, reason="absorption_flip", realized_r=realized_r):
            pending_store().cancel_for_position(pos.position_id)
            _va_state.pop(pos.position_id, None)
            _trend_escape_state.pop(pos.position_id, None)
            closed.append({"position_id": pos.position_id, "realized_r": round(realized_r, 3)})
            LOG.info(f"[cycle] ABSORPTION-FLIP {pos.position_id} {pos.side} @ {c:.2f} R={realized_r:.2f}")
    return closed


def _check_cvd_divergence_exit(symbol: str, latest: Bar, settings: dict | None = None) -> list[dict]:
    """Close any open cycle when CVD shows ORDER-FLOW EXHAUSTING against it — a
    divergence / hidden-flow / wick-trap signal on the OPPOSITE side, ≥ conf gate.

    Opt-in (caller gates on settings.cycle.cvd_divergence_exit). Long exits on a
    short-side exhaustion signal (divergence_bear / hidden_selling / wick_trap),
    short on the bull mirror. Realized R marked to the bar close like the other
    footprint exits. Used by the `reversal` strategy."""
    from execution.pending_orders import pending_store
    from execution.position_store import position_store
    from pipeline.state_store import store as _store
    from pipeline.features.cvd_candlestick import detect as _cvd_detect

    closed: list[dict] = []
    pos_store = position_store()
    opens = pos_store.open_positions(symbol)
    if not opens:
        return closed
    conf_gate = float((settings or {}).get("cycle", {}).get("cvd_exit_conf", 0.65))
    exhaustion = {"divergence_bear", "divergence_bull", "hidden_buying",
                  "hidden_selling", "wick_trap"}
    bars = _store().recent(symbol, latest.tf, 30)
    sig = _cvd_detect(bars) if len(bars) >= 6 else None
    if not sig or sig.pattern not in exhaustion or sig.confidence < conf_gate:
        return closed

    c = latest.ohlc.c
    for pos in opens:
        against = (pos.side == "long" and sig.side == "short") or \
                  (pos.side == "short" and sig.side == "long")
        if not against:
            continue
        risk = abs(pos.avg_entry - (pos.stop_loss or 0)) or 1e-9
        pnl = (c - pos.avg_entry) if pos.side == "long" else (pos.avg_entry - c)
        realized_r = pnl / risk
        if pos_store.close_position(pos.position_id, reason="cvd_divergence", realized_r=realized_r):
            pending_store().cancel_for_position(pos.position_id)
            _va_state.pop(pos.position_id, None)
            _trend_escape_state.pop(pos.position_id, None)
            closed.append({"position_id": pos.position_id, "realized_r": round(realized_r, 3),
                           "pattern": sig.pattern})
            LOG.info(f"[cycle] CVD-DIVERGENCE-EXIT {pos.position_id} {pos.side} @ {c:.2f} "
                     f"R={realized_r:.2f} ({sig.pattern} conf={sig.confidence:.2f})")
    return closed


def _check_si_trail(symbol: str, latest: Bar, settings: dict | None = None) -> list[dict]:
    """Trail the stop under each new bar's TRADE-DIRECTION stacked imbalance — a
    footprint chandelier. Long: SL → strongest BUY-stack low − buf; short: SELL-stack
    high + buf. Tighten ONLY (never loosen), and never cross price. Opt-in
    (settings.cycle.si_trail_exit). Used by `reversal_si`."""
    from execution.position_store import position_store
    from pipeline.footprint import build as _build_fp
    from pipeline.features.stacked_imbalance import stacked_imbalances
    from pipeline.features.atr import atr as _atr
    from pipeline.state_store import store as _store

    moved: list[dict] = []
    pstore = position_store()
    opens = pstore.open_positions(symbol)
    if not opens:
        return moved
    recent = _store().recent(symbol, latest.tf, 20)
    a = _atr(recent) if recent else 0.0
    buf = 0.05 * a
    zones = stacked_imbalances(_build_fp(latest), min_stack=3)
    c = latest.ohlc.c
    for pos in opens:
        want = "buy" if pos.side == "long" else "sell"
        zs = [z for z in zones if z.side == want]
        if not zs:
            continue
        z = max(zs, key=lambda z: z.count)
        new_sl = (z.price_low - buf) if pos.side == "long" else (z.price_high + buf)
        cur = pos.stop_loss
        tighter = (new_sl > cur) if pos.side == "long" else (new_sl < cur)
        safe = (new_sl < c) if pos.side == "long" else (new_sl > c)
        if tighter and safe:
            pstore.adjust_sl(pos.position_id, round(new_sl, 4), reason="si_trail")
            moved.append({"position_id": pos.position_id, "new_sl": round(new_sl, 4)})
            LOG.info(f"[cycle] SI-TRAIL {pos.position_id} {pos.side} SL {cur:.2f}→{new_sl:.2f}")
    return moved


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


def _check_scale_out(symbol: str, latest: Bar, settings: dict | None = None) -> list[dict]:
    """Aggressive scale-out (grid_sim.py 'aggressive keep2'). On first bounce
    PROFIT_BUF×ATR beyond avg_entry, bank the deep large-lot legs (filled cheap →
    now in profit), keep `keep_near` nearest legs, move them to break-even. One-shot
    per cycle. DEFAULT OFF — gated on cycle.scale_out.enabled.

    Partial R is reported in the cycle's existing denominator (avg_entry − stop_loss),
    scaled by the fraction of legs banked, so it stays additive with the eventual
    close R. Paper-path: this is a store event; live broker partial-close sync is a
    separate reconciler concern (TODO before live enable)."""
    cfg = (settings or {}).get("cycle", {}).get("scale_out", {})
    if not cfg.get("enabled", False):
        return []
    keep_near = int(cfg.get("keep_near", 2))
    profit_buf = float(cfg.get("profit_buf_atr", 0.5))

    from execution.position_store import position_store
    from pipeline.features.atr import atr_from_store
    atr15 = atr_from_store(symbol, "15m", period=14) or 0.0
    if atr15 <= 0:
        return []

    pos_store = position_store()
    done = []
    for pos in pos_store.open_positions(symbol):
        if pos.scaled_out or pos.leg_count <= keep_near:
            continue
        avg = pos.avg_entry
        c = latest.ohlc.c
        in_profit = (c > avg + profit_buf * atr15) if pos.side == "long" \
            else (c < avg - profit_buf * atr15)
        if not in_profit:
            continue
        # nearest-first by distance from leg1 (anchor); keep nearest, drop the deep
        legs_sorted = sorted(pos.legs, key=lambda L: abs(L.entry - pos.legs[0].entry))
        keep, drop = legs_sorted[:keep_near], legs_sorted[keep_near:]
        if not drop:
            continue
        avg_drop = sum(L.entry for L in drop) / len(drop)
        risk = abs(avg - pos.stop_loss) or 1e-9
        sign = 1 if pos.side == "long" else -1
        frac = len(drop) / pos.leg_count
        partial_r = sign * (c - avg_drop) / risk * frac
        be = sum(L.entry for L in keep) / len(keep)   # break-even = remaining avg
        if pos_store.scale_out_position(pos.position_id, [L.leg for L in drop],
                                        exit_price=c, realized_r=partial_r,
                                        new_sl=be, reason="scale_out_bounce"):
            done.append({"position_id": pos.position_id, "banked_legs": len(drop),
                         "partial_r": round(partial_r, 3), "be": round(be, 2)})
            LOG.info(f"[cycle] SCALE-OUT {pos.position_id} {pos.side} banked {len(drop)} "
                     f"legs @ {c:.2f} R={partial_r:+.3f}, {len(keep)} legs → BE {be:.2f}")
    return done


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
            # Disaster floor: require adverse move ≥ 3 % (BTC) / 1.5 % (XAU)
            # of avg_entry before forcing close. Within floor → hold and let
            # grid DCA / pending legs recover the cycle.
            _floor_pct = 0.03 if symbol.startswith("BTC") else 0.015
            if adverse_distance < pos.avg_entry * _floor_pct:
                continue
            risk = abs(pos.avg_entry - pos.stop_loss) or 1e-9
            realized_r = -adverse_distance / risk
            reason = (f"trend-escape (disaster): {st['bars_against']} bars against, "
                      f"{adverse_distance:.2f}pts ≥ {N_atr:.1f}×ATR ({N_atr * atr_15:.2f}) "
                      f"+ ≥ {_floor_pct*100:.1f}% disaster floor")
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
        actions["scaled_out"] = _check_scale_out(symbol, latest, settings)
    except Exception as e:
        LOG.exception(f"[cycle] scale_out failed: {e}")
        actions["scale_out_error"] = str(e)
    try:
        actions["closed_tp"] = _check_cycle_tp(symbol, latest)
    except Exception as e:
        LOG.exception(f"[cycle] check_tp failed: {e}")
        actions["tp_error"] = str(e)

    # Hard-SL exit — OPT-IN (settings.cycle.hard_sl_exit). Default OFF preserves
    # the no-hard-SL / disaster-floor-only policy. When on (e.g. the `republic`
    # strategy), close a cycle the bar its low/high crosses pos.stop_loss.
    if (settings or {}).get("cycle", {}).get("hard_sl_exit"):
        try:
            actions["closed_sl"] = _check_hard_sl(symbol, latest)
        except Exception as e:
            LOG.exception(f"[cycle] hard_sl check failed: {e}")
            actions["hard_sl_error"] = str(e)

    # Absorption-flip exit — OPT-IN (settings.cycle.coup_flip_exit). The `coup`
    # strategy exits when its winner side gets absorbed at an extreme (the entry
    # trigger mirrored against the open position). Default OFF; other strategies
    # untouched.
    if (settings or {}).get("cycle", {}).get("coup_flip_exit"):
        try:
            actions["closed_flip"] = _check_absorption_flip(symbol, latest)
        except Exception as e:
            LOG.exception(f"[cycle] absorption_flip check failed: {e}")
            actions["flip_error"] = str(e)

    # CVD-divergence exit — OPT-IN (settings.cycle.cvd_divergence_exit). Cut a cycle
    # when CVD exhaustion prints against it. Used by the `reversal` strategy.
    if (settings or {}).get("cycle", {}).get("cvd_divergence_exit"):
        try:
            actions["closed_cvd"] = _check_cvd_divergence_exit(symbol, latest, settings)
        except Exception as e:
            LOG.exception(f"[cycle] cvd_divergence check failed: {e}")
            actions["cvd_exit_error"] = str(e)

    # SI-trail — OPT-IN (settings.cycle.si_trail_exit). Footprint chandelier stop for
    # the `reversal_si` strategy: tighten SL under each bar's stacked imbalance; the
    # tightened stop triggers on a later bar's hard-SL check.
    if (settings or {}).get("cycle", {}).get("si_trail_exit"):
        try:
            actions["si_trailed"] = _check_si_trail(symbol, latest, settings)
        except Exception as e:
            LOG.exception(f"[cycle] si_trail failed: {e}")
            actions["si_trail_error"] = str(e)

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

    # POC-trail TP — as intraday POC migrates toward avg_entry, trail TP to it.
    # Prevents cycles from sitting locked to a static VP level while price has
    # already rotated to a different equilibrium. Only trails CLOSER (not farther).
    # Floor: TP must stay ≥ poc_trail_min_atr_mult × ATR from avg_entry.
    try:
        from pipeline.features.vp_cache import get as _vp_get2
        from pipeline.features.atr import atr as _atr2
        from pipeline.state_store import store as _store2
        from execution.position_store import position_store as _pstore2_factory
        _pstore2 = _pstore2_factory()
        _recent2 = _store2().recent(symbol, primary_tf, 20)
        _atr2_val = _atr2(_recent2) if _recent2 else None
        _vp2 = _vp_get2(symbol, "daily")
        _poc2 = float(_vp2["poc"]) if _vp2 and _vp2.get("poc") else None
        _poc_trail_mult = float(
            (settings.get("cycle") or {}).get("poc_trail_min_atr_mult", 0.5)
        )
        if _poc2 is not None and _atr2_val:
            _max_ext_mult = float(
                (settings.get("cycle") or {}).get("poc_trail_max_ext_atr_mult", 3.0)
            )
            for _pos2 in _pstore2.open_positions(symbol):
                if _pos2.tp is None:
                    continue
                _min_dist = _poc_trail_mult * _atr2_val
                _max_ext  = _max_ext_mult * _atr2_val
                if _pos2.side == "long":
                    _floor2 = _pos2.avg_entry + _min_dist
                    _ext_cap = _pos2.avg_entry + _max_ext
                    if _floor2 < _poc2 < _pos2.tp:
                        # POC migrated closer — tighten TP
                        _pstore2.adjust_tp(_pos2.position_id, _poc2, "poc_trail")
                        LOG.info(
                            f"[cycle][poc_trail] {symbol} pos={_pos2.position_id} "
                            f"tp {_pos2.tp:.2f}→{_poc2:.2f} (poc tighter)"
                        )
                    elif _poc2 > _pos2.tp and _poc2 <= _ext_cap:
                        # POC drifted farther — extend TP to capture more
                        _pstore2.adjust_tp(_pos2.position_id, _poc2, "poc_trail_extend")
                        LOG.info(
                            f"[cycle][poc_trail_extend] {symbol} pos={_pos2.position_id} "
                            f"tp {_pos2.tp:.2f}→{_poc2:.2f} (poc drifted up)"
                        )
                else:
                    _floor2 = _pos2.avg_entry - _min_dist
                    _ext_cap = _pos2.avg_entry - _max_ext
                    if _pos2.tp < _poc2 < _floor2:
                        # POC migrated closer — tighten TP
                        _pstore2.adjust_tp(_pos2.position_id, _poc2, "poc_trail")
                        LOG.info(
                            f"[cycle][poc_trail] {symbol} pos={_pos2.position_id} "
                            f"tp {_pos2.tp:.2f}→{_poc2:.2f} (poc tighter)"
                        )
                    elif _poc2 < _pos2.tp and _poc2 >= _ext_cap:
                        # POC drifted farther — extend TP to capture more
                        _pstore2.adjust_tp(_pos2.position_id, _poc2, "poc_trail_extend")
                        LOG.info(
                            f"[cycle][poc_trail_extend] {symbol} pos={_pos2.position_id} "
                            f"tp {_pos2.tp:.2f}→{_poc2:.2f} (poc drifted down)"
                        )
    except Exception as e:
        LOG.debug(f"[cycle] poc_trail failed: {e}")

    # SL trailing DISABLED — no-SL cycle policy. Trailing tightens stop_loss
    # toward current price; once tight enough, intra-bar moves fire it within
    # disaster floor. Cycle manages itself via scaled future orders + TP.
    actions["sl_trailed"] = []

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

    # Hedge-eval makes a live Claude call. Default ON (legacy). Scoped strategy
    # ticks set settings.cycle.hedge_eval_enabled=False so a paper A/B stays
    # deterministic and free.
    _hedge_enabled = (settings or {}).get("cycle", {}).get("hedge_eval_enabled", True)
    if invalidated and settings and _hedge_enabled:
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
