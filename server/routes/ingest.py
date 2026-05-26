"""POST /ingest — store bar, check open positions for exits.

On every bar:
1. Normalize payload → canonical Bar
2. Store in state_store (idempotent)
3. Aggregate MTF
4. Check all open positions for this symbol:
   a. SL hit (price touched SL)
   b. TP absorption (opposite-side absorption near TP)
   c. Footprint invalidation (opposite absorption AT entry)
   d. Daily DD circuit breaker

Decisions (Claude calls) are invoked separately via POST /decide.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, jsonify, request

LOG = logging.getLogger(__name__)

from pipeline.footprint import build as build_fp
from pipeline.mtf_aggregator import maybe_emit
from pipeline.normalizer import normalize
from pipeline.state_store import store
from pipeline.features.invalidation import detect_invalidation, check_tp_absorption
from pipeline.features.vp_history import snapshot_if_boundary
from execution.position_store import position_store
from execution.sl_manager import check_sl_adjustments

bp = Blueprint("ingest", __name__)

_bar_locks: dict[str, Lock] = defaultdict(Lock)
_bar_locks_meta = Lock()


def _lock_for(bar_id: str) -> Lock:
    with _bar_locks_meta:
        return _bar_locks[bar_id]


def _aggregate_mtf(bar, settings) -> None:
    primary_tf = settings["instrument"]["primary_tf"]
    if bar.tf != primary_tf:
        return
    s = store()
    primary_bars = s.recent(bar.symbol, primary_tf, 1_000)
    for tf in settings["instrument"]["timeframes"]:
        if tf == primary_tf:
            continue
        synth = maybe_emit(primary_bars, primary_tf, tf)
        if synth is not None:
            s.put(synth)


MIN_HOLD_SECS   = 180   # seconds a position must be open before invalidation/hedge fires
MIN_HEDGE_SECS  = 300   # seconds a hedge must be held before recovery removal is allowed


def _check_positions(bar, settings) -> list[dict]:
    """Check open positions for SL/TP/invalidation on this bar. Returns list of exit events."""
    ps = position_store()
    exits = []
    max_dd = float(settings.get("risk", {}).get("daily", {}).get("max_dd_r", 99))

    # Daily DD circuit breaker — close everything (store + broker) and halt
    if ps.daily_realized_r() < -abs(max_dd):
        for pos in ps.open_positions(bar.symbol):
            # Close at broker first for live tickets, then mark in store
            if pos.broker_ticket:
                try:
                    from execution.live.mt5_adapter import MT5Adapter as _MT5
                    _MT5().close_position(pos.broker_ticket)
                    LOG.warning(f"[ingest] DD halt broker close {pos.broker_ticket}")
                except Exception as e:
                    LOG.error(f"[ingest] DD halt broker close FAILED {pos.broker_ticket}: {e}")
            ps.invalidate_position(pos.position_id, "daily DD circuit breaker triggered")
            exits.append({"position_id": pos.position_id, "exit": "daily_dd_halt", "broker_ticket": pos.broker_ticket})
            LOG.warning(f"[ingest] Daily DD halt — closing {pos.position_id}")
        _close_cycles_for_exits([{"exit": "invalidated", "position_id": e["position_id"]} for e in exits])
        if exits:
            try:
                from utils.notify import notify
                notify("🛑 DAILY DD HALT", f"{bar.symbol} daily R {ps.daily_realized_r():.2f} ≤ -{max_dd}\nClosed {len(exits)} position(s), re-entry blocked")
            except Exception:
                pass
        return exits

    # Circuit breaker: net unrealized R ≤ limit OR exposure cap → force-close all
    try:
        from execution.hedge_manager import check_circuit_breaker
        cb_hit, cb_reason = check_circuit_breaker(bar.symbol, settings, ps, current_price=bar.ohlc.c)
        if cb_hit:
            for pos in ps.open_positions(bar.symbol):
                if pos.broker_ticket:
                    try:
                        from execution.live.mt5_adapter import MT5Adapter as _MT5
                        _MT5().close_position(pos.broker_ticket)
                    except Exception as e:
                        LOG.error(f"[ingest] circuit breaker broker close FAILED {pos.broker_ticket}: {e}")
                ps.invalidate_position(pos.position_id, f"circuit breaker: {cb_reason}")
                exits.append({"position_id": pos.position_id, "exit": "circuit_breaker", "reason": cb_reason})
                LOG.warning(f"[ingest] CIRCUIT BREAKER close {pos.position_id}: {cb_reason}")
            if exits:
                _close_cycles_for_exits([{"exit": "invalidated", "position_id": e["position_id"]} for e in exits])
                try:
                    from utils.notify import notify
                    notify("⛔ CIRCUIT BREAKER", f"{bar.symbol}: {cb_reason}\nForce-closed {len(exits)} position(s)")
                except Exception:
                    pass
                return exits
    except Exception as e:
        LOG.warning(f"[ingest] circuit breaker check failed: {e}")

    fp = build_fp(bar)

    for pos in ps.open_positions(bar.symbol):
        risk = abs(pos.avg_entry - pos.stop_loss)

        sl_hit = (
            (pos.side == "long"  and bar.ohlc.l <= pos.stop_loss) or
            (pos.side == "short" and bar.ohlc.h >= pos.stop_loss)
        )
        # Grid TP guard: when trading_mode is buy_sell_only / grid, the position's
        # stored TP is computed for the full-fill avg_entry. While only leg-1 is
        # filled, avg_entry sits on the wrong side of TP and would trigger a
        # loss-close. Skip the TP check until avg_entry crosses to the profitable side.
        _grid_mode = str(settings.get("trading_mode") or "buy_sell_only") in ("buy_sell_only", "grid")
        tp_guard_skips = _grid_mode and (
            (pos.side == "long"  and pos.take_profit <= pos.avg_entry) or
            (pos.side == "short" and pos.take_profit >= pos.avg_entry)
        )
        tp_hit = (not tp_guard_skips) and (
            (pos.side == "long"  and bar.ohlc.h >= pos.take_profit) or
            (pos.side == "short" and bar.ohlc.l <= pos.take_profit)
        )

        # 1. Hard SL hit — if same bar hits both SL and TP, SL wins (conservative)
        if sl_hit:
            realized_r = -1.0  # always exactly -1R by definition
            if pos.side == "long":
                ps.close_position(pos.position_id, f"sl_hit @ {bar.ohlc.l:.4f} ≤ SL {pos.stop_loss:.4f}", realized_r)
                LOG.info(f"[ingest] SL hit {pos.position_id} long bar_low={bar.ohlc.l:.4f} sl={pos.stop_loss:.4f}")
            else:
                ps.close_position(pos.position_id, f"sl_hit @ {bar.ohlc.h:.4f} ≥ SL {pos.stop_loss:.4f}", realized_r)
                LOG.info(f"[ingest] SL hit {pos.position_id} short bar_high={bar.ohlc.h:.4f} sl={pos.stop_loss:.4f}")
            exits.append({"position_id": pos.position_id, "exit": "sl_hit", "realized_r": realized_r,
                          "bar_extreme": bar.ohlc.l if pos.side == "long" else bar.ohlc.h,
                          "sl": pos.stop_loss})

        # 2. Hard TP hit — price actually reached the target level
        elif tp_hit:
            realized_r = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 1.5
            if pos.side == "long":
                ps.close_position(pos.position_id, f"tp_hit @ {bar.ohlc.h:.4f} ≥ TP {pos.take_profit:.4f}", realized_r)
                LOG.info(f"[ingest] TP hit {pos.position_id} long bar_high={bar.ohlc.h:.4f} tp={pos.take_profit:.4f} R={realized_r:.2f}")
                try:
                    from execution.pending_orders import pending_store as _ps2
                    _ps2().cancel_for_position(pos.position_id)
                except Exception:
                    pass
            else:
                ps.close_position(pos.position_id, f"tp_hit @ {bar.ohlc.l:.4f} ≤ TP {pos.take_profit:.4f}", realized_r)
                LOG.info(f"[ingest] TP hit {pos.position_id} short bar_low={bar.ohlc.l:.4f} tp={pos.take_profit:.4f} R={realized_r:.2f}")
                try:
                    from execution.pending_orders import pending_store as _ps2
                    _ps2().cancel_for_position(pos.position_id)
                except Exception:
                    pass
            exits.append({"position_id": pos.position_id, "exit": "tp_hit", "realized_r": realized_r,
                          "bar_extreme": bar.ohlc.h if pos.side == "long" else bar.ohlc.l,
                          "tp": pos.take_profit})

        # 3. TP absorption exit — footprint signal confirmed price reached TP zone (95% gate)
        elif tp_reason := check_tp_absorption(bar, fp, pos.side, pos.take_profit, entry=pos.avg_entry):
            if risk > 0:
                # Exit price = actual bar extreme capped at TP (can't fill beyond TP)
                exit_price = min(bar.ohlc.h, pos.take_profit) if pos.side == "long" else max(bar.ohlc.l, pos.take_profit)
                realized_r = abs(exit_price - pos.avg_entry) / risk
            else:
                realized_r = 1.5
            ps.close_position(pos.position_id, f"tp_absorption: {tp_reason}", realized_r)
            exits.append({"position_id": pos.position_id, "exit": "tp_absorption", "realized_r": round(realized_r, 4), "reason": tp_reason})
            LOG.info(f"[ingest] TP absorption {pos.position_id} exit={exit_price:.4f} R={realized_r:.2f}: {tp_reason}")

        # 4. Footprint invalidation → hedge (only after MIN_HOLD_SECS, avoids sub-bar fires)
        elif (bar.close_ts - pos.opened_ts >= MIN_HOLD_SECS and
              (inv := detect_invalidation(bar, fp, pos.side, pos.avg_entry))):
            if inv.strength == "strong":
                try:
                    from execution.hedge_manager import open_hedge, active_hedge_for_cycle
                    from execution.cycle_store import cycle_store
                    cs = cycle_store()
                    active = [c for c in cs.active_cycles(bar.symbol)
                              if c.position_id == pos.position_id]
                    if active and not active_hedge_for_cycle(active[0].cycle_id):
                        lots = float(settings.get("risk", {}).get("base_lot_size", 0.01)) * pos.leg_count
                        hedge = open_hedge(active[0].cycle_id, bar.symbol, pos.side, lots, bar.ohlc.c)

                        # Fire opposite-side order through normal dispatch path
                        # (lives + paper + journal all handled — gated by settings.mode).
                        try:
                            from execution.router import dispatch as _dispatch
                            from llm.schema import Decision as _SynthDecision
                            sl_dist = abs(pos.avg_entry - pos.stop_loss) or (bar.ohlc.c * 0.002)
                            if hedge.side == "long":
                                h_sl = round(bar.ohlc.c - sl_dist, 4)
                                h_tp = round(bar.ohlc.c + sl_dist * 2, 4)
                            else:
                                h_sl = round(bar.ohlc.c + sl_dist, 4)
                                h_tp = round(bar.ohlc.c - sl_dist * 2, 4)
                            synth = _SynthDecision(
                                side=hedge.side,
                                entry=round(bar.ohlc.c, 4),
                                stop_loss=h_sl,
                                take_profit=h_tp,
                                confidence=1.0,
                                rationale=f"hedge cycle={active[0].cycle_id} reason={inv.reason}",
                            )
                            h_result = _dispatch(synth, bar, settings)
                            if isinstance(h_result, dict):
                                hedge.position_id = str(h_result.get("position_id") or "")
                                order = h_result.get("order") or {}
                                hedge.broker_ticket = str(order.get("positionId") or order.get("orderId") or "")
                            LOG.info(
                                f"[ingest] HEDGE order {hedge.hedge_id} → ticket={hedge.broker_ticket} pid={hedge.position_id}"
                            )
                        except Exception as e:
                            LOG.warning(f"[ingest] hedge order dispatch failed for {hedge.hedge_id}: {e}")

                        exits.append({
                            "position_id": pos.position_id,
                            "exit": "hedged",
                            "hedge_id": hedge.hedge_id,
                            "broker_ticket": hedge.broker_ticket,
                            "reason": inv.reason,
                        })
                        LOG.info(f"[ingest] HEDGED {pos.position_id} cycle={active[0].cycle_id}: {inv.reason}")
                        try:
                            from utils.notify import notify
                            notify("🔵 HEDGE OPENED", f"{bar.symbol} {hedge.side} {hedge.lots} lot @ {bar.ohlc.c}\nticket {hedge.broker_ticket}\n{inv.reason}")
                        except Exception:
                            pass
                    else:
                        # No active cycle tracked — fall back to invalidate
                        ps.invalidate_position(pos.position_id, inv.reason)
                        exits.append({"position_id": pos.position_id, "exit": "invalidated", "reason": inv.reason})
                        LOG.info(f"[ingest] INVALIDATED {pos.position_id}: {inv.reason}")
                except Exception:
                    ps.invalidate_position(pos.position_id, inv.reason)
                    exits.append({"position_id": pos.position_id, "exit": "invalidated", "reason": inv.reason})

        # 5. Check if active hedge can be removed (price recovered, after MIN_HEDGE_SECS)
        else:
            try:
                from execution.hedge_manager import active_hedge_for_cycle, should_remove_hedge, remove_hedge
                from execution.cycle_store import cycle_store
                cs = cycle_store()
                for cyc in cs.active_cycles(bar.symbol):
                    if cyc.position_id != pos.position_id:
                        continue
                    hedge = active_hedge_for_cycle(cyc.cycle_id)
                    if hedge:
                        # Don't remove hedge until it has been held for MIN_HEDGE_SECS
                        if bar.close_ts - hedge.opened_ts < MIN_HEDGE_SECS:
                            continue
                        ok, reason = should_remove_hedge(bar, fp, hedge, pos.side, wave_cvd_quality=None)
                        if ok:
                            # Close the opposite-side ticket at broker first (live only).
                            # Paper / journal modes have no ticket; skip silently.
                            if hedge.broker_ticket:
                                try:
                                    from execution.live.mt5_adapter import MT5Adapter as _MT5
                                    close_res = _MT5().close_position(hedge.broker_ticket)
                                    LOG.info(f"[ingest] hedge broker close {hedge.broker_ticket} → {close_res}")
                                except Exception as e:
                                    LOG.warning(f"[ingest] hedge broker close failed {hedge.broker_ticket}: {e}")
                            remove_hedge(hedge.hedge_id, reason)
                            exits.append({
                                "position_id": pos.position_id,
                                "exit": "hedge_removed",
                                "broker_ticket": hedge.broker_ticket,
                                "reason": reason,
                            })
                            LOG.info(f"[ingest] HEDGE REMOVED {hedge.hedge_id}: {reason}")
            except Exception:
                pass

    # Close any cycle whose position was closed/invalidated this bar
    _close_cycles_for_exits(exits)
    return exits


def _close_cycles_for_exits(exits: list[dict]) -> None:
    """Mirror position close → cycle close. Closes the cycle linked to any
    position that exited via sl/tp/invalidation this bar. Hedge exits are not
    cycle-terminal (the cycle continues / recovers)."""
    if not exits:
        return
    terminal = {"sl_hit", "tp_hit", "tp_absorption", "invalidated"}
    try:
        from execution.cycle_store import cycle_store
        from execution.position_store import position_store
        cs = cycle_store()
        ps = position_store()
        for ev in exits:
            if ev.get("exit") not in terminal:
                continue
            pid = ev.get("position_id")
            cyc = cs.by_position_id(pid) if pid else None
            if cyc:
                pos = next((p for p in ps._positions.values() if p.position_id == pid), None)
                realized = pos.realized_r if pos else ev.get("realized_r", 0.0)
                cs.close_cycle(cyc.cycle_id, realized_pnl=realized, reason=ev.get("exit", ""))
    except Exception as e:
        LOG.warning(f"[ingest] cycle close mirror failed: {e}")


@bp.post("/ingest")
def ingest():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        bar = normalize(payload)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    settings = current_app.config["FB_SETTINGS"]
    primary_tf = settings["instrument"]["primary_tf"]

    with _lock_for(bar.bar_id):
        s = store()
        prev = s.latest(bar.symbol, bar.tf)
        was_new = s.put(bar)
        if not was_new:
            return jsonify({"ok": True, "duplicate": True, "bar_id": bar.bar_id})
        _aggregate_mtf(bar, settings)

        # Snapshot VP + refresh cache + write daily journal at day boundary
        if prev and bar.tf == primary_tf:
            snapped = snapshot_if_boundary(prev.close_ts, bar.close_ts, bar.symbol, primary_tf)
            if snapped:
                LOG.info(f"[ingest] VP snapshot: {bar.symbol} {snapped}")
                from pipeline.features.vp_cache import build_and_save
                _vp_cfg = settings.get("vp_cache", {}) or {}
                build_and_save(
                    [bar.symbol], primary_tf,
                    session_start_utc=_vp_cfg.get("session_start_utc", {}),
                    vp_bin_size=_vp_cfg.get("vp_bin_size", {}),
                )
                # Write journal for the day that just closed
                if snapped.get("daily"):
                    try:
                        from pipeline.features.daily_journal import write_day_journal
                        sess_anchor = (settings.get("vp_cache", {}).get("session_start_utc", {}) or {}).get(bar.symbol, 0)
                        closed_date = snapped["daily"]
                        result = write_day_journal(bar.symbol, primary_tf, closed_date, sess_anchor)
                        if result:
                            LOG.info(f"[ingest] Daily journal written: {result.name}")
                    except Exception:
                        pass

    exits = _check_positions(bar, settings)

    # Reconcile live broker state for tradable symbols (broker-side SL/TP closes)
    if bar.tf == primary_tf:
        _exec_cfg = settings.get("execution", {}) or {}
        _tradable = set(_exec_cfg.get("tradable_symbols") or [])
        if bar.symbol in _tradable:
            try:
                from execution.live.mt5_adapter import MT5Adapter
                from execution.reconcile import reconcile as _reconcile
                summary = _reconcile(MT5Adapter(), bar.symbol)
                if summary.get("closed", 0) > 0:
                    LOG.info(f"[ingest] reconcile {bar.symbol}: closed {summary['closed']} positions")
            except Exception as e:
                LOG.warning(f"[ingest] reconcile {bar.symbol} failed: {e}")

    # Trail SL / break-even after positions checked (so we don't move SL on a bar that just closed)
    recent_bars = store().recent(bar.symbol, bar.tf, 10)
    sl_adjustments = check_sl_adjustments(bar, recent_bars)

    # Update swing point cache (used by sweep detector in builder.py)
    if bar.tf == primary_tf:
        try:
            from pipeline.features.swing import update as _swing_update
            from pipeline.features.vp_cache import _session_day_key, _day_bounds
            sess_anchor = settings.get("vp_cache", {}).get(
                "session_start_utc", {}
            ).get(bar.symbol, 0)
            # DST-aware: resolve the IST session label for "now", then look up its UTC start
            import time as _time
            _now_ts = int(_time.time())
            _label = _session_day_key(_now_ts, sess_anchor)
            _sess_ts, _ = _day_bounds(_label, sess_anchor)
            _swing_update(bar.symbol, primary_tf, _sess_ts)
        except Exception:
            pass

    # Classify big trade outcomes for pending events
    if bar.tf == primary_tf:
        try:
            from pipeline.features.big_trade import classify_outcomes as _classify_outcomes
            _classify_outcomes(recent_bars, bar.symbol)
        except Exception:
            pass

    # Cycle manager heartbeat (fills pending legs, checks TP, ChoCh + VA invalidation, hedge eval)
    if bar.tf == primary_tf:
        try:
            from execution.cycle_manager import on_bar_close as _cycle_tick
            _cycle_actions = _cycle_tick(bar.symbol, primary_tf, bar, settings)
            if _cycle_actions and (_cycle_actions.get("filled") or _cycle_actions.get("closed_tp") or _cycle_actions.get("hedge_evals")):
                import logging as _l
                _l.getLogger(__name__).info(f"[ingest][cycle] {_cycle_actions}")
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning(f"[ingest][cycle] tick failed: {e}")

    return jsonify({
        "ok": True,
        "bar_id": bar.bar_id,
        "symbol": bar.symbol,
        "tf": bar.tf,
        "delta": bar.delta,
        "exits": exits,
        "sl_adjustments": [{"position_id": a.position_id, "old": a.old_sl, "new": a.new_sl, "reason": a.reason} for a in sl_adjustments],
    })
