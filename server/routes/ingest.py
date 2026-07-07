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


_triggered_decide_bars: set[str] = set()


def _trigger_decide_on_bar_close(symbol: str, tf: str, bar_id: str) -> None:
    """Fire Mode 1 (Claude) + Mode 2 (rules dry-run) instantly on bar close.

    Claude call (/decide_multi) gated on ClaudeMode.FULL — RESTRICTED and OFF
    modes skip the per-bar decide; cycle_manager handles RESTRICTED cycle-init calls.

    Idempotent per bar_id. Non-blocking — runs HTTP calls in a background thread
    so ingest returns immediately.
    """
    if bar_id in _triggered_decide_bars:
        return
    _triggered_decide_bars.add(bar_id)
    if len(_triggered_decide_bars) > 500:
        kept = list(_triggered_decide_bars)[-250:]
        _triggered_decide_bars.clear()
        _triggered_decide_bars.update(kept)

    import threading
    import urllib.request as _req
    import json as _json

    # Gate Claude per-bar call
    try:
        from execution.claude_mode import is_per_bar
        _claude_per_bar = is_per_bar()
    except Exception:
        _claude_per_bar = True  # safe default: allow if module missing

    def _run():
        endpoints: list[tuple[str, dict[str, object], int]] = []
        if _claude_per_bar:
            endpoints.append(("/decide_multi", {"symbols": [symbol], "tf": tf}, 120))
        endpoints.append(("/grid_tick", {"symbols": [symbol], "tf": tf, "dry_run": True}, 30))
        for endpoint, body, timeout in endpoints:
            try:
                data = _json.dumps(body).encode()
                req = _req.Request(
                    f"http://localhost:5000{endpoint}",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _req.urlopen(req, timeout=timeout).read()
            except Exception as e:
                LOG.warning(f"[ingest][trigger] {endpoint} failed for {symbol} {bar_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    LOG.info(f"[ingest][trigger] event-driven decide fired for {symbol} {tf} {bar_id} claude_per_bar={_claude_per_bar}")


def _aggregate_mtf(bar, settings) -> None:
    primary_tf = settings["instrument"]["primary_tf"]
    if bar.tf != primary_tf:
        return
    s = store()
    primary_bars = s.recent(bar.symbol, primary_tf, 1_000)
    _dobc = settings.get("decide_on_bar_close") or {}
    decide_tf = str(_dobc.get("tf", "15m"))
    # Strategy ENTRY ticks fire on each of these synthetic closes; the manager gates
    # each strategy to enter only on its own decide_tf/vote_tf (so a 5m and a 15m
    # instance can run in parallel). Default to the legacy single decide_tf.
    decide_tfs = set(_dobc.get("tfs") or [decide_tf])
    enabled = bool(_dobc.get("enabled", True))
    for tf in settings["instrument"]["timeframes"]:
        if tf == primary_tf:
            continue
        synth = maybe_emit(primary_bars, primary_tf, tf)
        if synth is not None:
            s.put(synth)
            if enabled and tf in decide_tfs:
                # Claude per-bar decide only on the canonical decide_tf (15m).
                if tf == decide_tf:
                    _trigger_decide_on_bar_close(bar.symbol, tf, synth.bar_id)
                # Strategy-manager ENTRY tick on this synthetic close. The /ingest
                # body only sees the 1m bar (manage-only), so entries must fire here.
                try:
                    from strategies.manager import get_manager
                    get_manager(settings).tick(
                        synth.symbol, tf, synth, settings, allow_entry=True,
                    )
                except Exception as e:
                    LOG.warning(f"[ingest][strategies] synth entry tick failed: {e}")
            # Sweep detection for synthetic bars (e.g. 15m)
            try:
                from pipeline.features.sweep import detect as _sw_det2, tick_registry as _sw_tick2, log_sweep as _sw_log2
                from pipeline.features.swing import update as _sw_upd2, get as _sw_get2, build as _sw_bld2
                _sp2 = _sw_get2(synth.symbol) or _sw_bld2(synth.symbol, tf, s.recent(synth.symbol, tf, 100))
                if _sp2 is not None:
                    _prev2 = s.recent(synth.symbol, tf, 20)
                    _sig2 = _sw_det2(synth, _sp2, prev_bars=_prev2[:-1])
                    if _sig2.type != "none":
                        _sw_log2(synth.symbol, _sig2, synth.close_ts, tf)
                _sw_tick2(synth.symbol, synth)
            except Exception:
                pass


MIN_HOLD_SECS   = 180   # seconds a position must be open before invalidation/hedge fires
MIN_HEDGE_SECS  = 300   # seconds a hedge must be held before recovery removal is allowed


def _check_positions(bar, settings) -> list[dict]:
    """Check open positions for SL/TP/invalidation on this bar. Returns list of exit events."""
    ps = position_store()
    exits = []

    fp = build_fp(bar)

    for pos in ps.open_positions(bar.symbol):
        risk = abs(pos.avg_entry - pos.stop_loss)

        # SL acts as DISASTER FLOOR only — must be far away from entry
        # (≥ 3 % for BTC, ≥ 1.5 % for XAU). Closer SL is ignored; cycle exits
        # only via TP / absorption / explicit invalidation. Prevents the
        # "−1 R repeatedly" pattern that broke expectancy.
        _floor_pct = 0.03 if pos.symbol.startswith("BTC") else 0.015
        _is_disaster = risk >= pos.avg_entry * _floor_pct
        _raw_sl_hit = (
            (pos.side == "long"  and bar.ohlc.l <= pos.stop_loss) or
            (pos.side == "short" and bar.ohlc.h >= pos.stop_loss)
        )
        sl_hit = _is_disaster and _raw_sl_hit
        if _raw_sl_hit and not _is_disaster:
            LOG.info(
                f"[ingest] SL skipped (within floor) {pos.position_id} {pos.symbol} "
                f"risk={risk:.2f} threshold={pos.avg_entry * _floor_pct:.2f}"
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

        # 1. Hard SL hit — if same bar hits both SL and TP, SL wins (conservative).
        # Realized R against DISASTER-FLOOR initial risk (3% BTC / 1.5% XAU)
        # so trailed SLs that lock profit show +R, not hardcoded -1R.
        if sl_hit:
            disaster_risk = pos.avg_entry * _floor_pct or risk or 1e-9
            if pos.side == "long":
                realized_r = (pos.stop_loss - pos.avg_entry) / disaster_risk
                ps.close_position(pos.position_id, f"sl_hit @ {bar.ohlc.l:.2f} ≤ SL {pos.stop_loss:.2f}", realized_r)
                LOG.info(f"[ingest] SL hit {pos.position_id} long bar_low={bar.ohlc.l:.2f} sl={pos.stop_loss:.2f} R={realized_r:+.2f}")
            else:
                realized_r = (pos.avg_entry - pos.stop_loss) / disaster_risk
                ps.close_position(pos.position_id, f"sl_hit @ {bar.ohlc.h:.2f} ≥ SL {pos.stop_loss:.2f}", realized_r)
                LOG.info(f"[ingest] SL hit {pos.position_id} short bar_high={bar.ohlc.h:.2f} sl={pos.stop_loss:.2f} R={realized_r:+.2f}")
            exits.append({"position_id": pos.position_id, "exit": "sl_hit", "realized_r": realized_r,
                          "bar_extreme": bar.ohlc.l if pos.side == "long" else bar.ohlc.h,
                          "sl": pos.stop_loss})

        # 2. Hard TP hit — price actually reached the target level
        elif tp_hit:
            realized_r = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 1.5
            if pos.side == "long":
                ps.close_position(pos.position_id, f"tp_hit @ {bar.ohlc.h:.2f} ≥ TP {pos.take_profit:.2f}", realized_r)
                LOG.info(f"[ingest] TP hit {pos.position_id} long bar_high={bar.ohlc.h:.2f} tp={pos.take_profit:.2f} R={realized_r:.2f}")
                try:
                    from execution.pending_orders import pending_store as _ps2
                    _ps2().cancel_for_position(pos.position_id)
                except Exception:
                    pass
            else:
                ps.close_position(pos.position_id, f"tp_hit @ {bar.ohlc.l:.2f} ≤ TP {pos.take_profit:.2f}", realized_r)
                LOG.info(f"[ingest] TP hit {pos.position_id} short bar_low={bar.ohlc.l:.2f} tp={pos.take_profit:.2f} R={realized_r:.2f}")
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
            LOG.info(f"[ingest] TP absorption {pos.position_id} exit={exit_price:.2f} R={realized_r:.2f}: {tp_reason}")

        # 4. Strong-zone profit booking on absorption
        #    Replaces the legacy "absorption-at-entry → hedge/invalidate" path.
        #    Rules (side-mirrored):
        #      - Detect absorption on this bar (sell absorption for long, buy for short)
        #      - If absorption price is at a strong zone (HVN/POC/VAH/VAL/FVG) AND
        #        the cycle has positive PnL → book profit at current price
        #      - If at strong zone but PnL negative → HOLD (let trail_sl handle)
        #      - If NOT at strong zone → IGNORE (no auto-close on absorption alone)
        elif _grid_mode and bar.close_ts - pos.opened_ts >= MIN_HOLD_SECS:
            try:
                from pipeline.features.absorption import detect_absorption
                from execution.zone_collector import _all_zones
                absorps = detect_absorption(bar, fp, absorb_ratio=0.20)
                cur_price = bar.ohlc.c
                pnl_positive = ((pos.side == "long" and cur_price > pos.avg_entry) or
                                (pos.side == "short" and cur_price < pos.avg_entry))
                # Which absorption side threatens this cycle?
                relevant_side = "sell" if pos.side == "long" else "buy"
                relevant = [a for a in absorps if a.side == relevant_side]
                if relevant and pnl_positive:
                    # Get all strong zones (need to grab htf_bars for FVG)
                    htf_bars = s.recent(bar.symbol, "15m", 200)
                    zones = _all_zones(bar.symbol, htf_bars=htf_bars)
                    tol = max(bar.ohlc.c * 0.001, 0.5)   # 0.1% zone tolerance
                    a = relevant[0]
                    at_zone = next((z for z in zones if abs(a.price - z.price) <= tol and z.strength >= 0.7), None)
                    if at_zone:
                        realized_r = abs(cur_price - pos.avg_entry) / risk if risk > 0 else 0.5
                        reason = (f"strong_zone_book: {relevant_side} absorption {a.bar_pct:.0%} at "
                                  f"{a.price:.2f} = {at_zone.source}, PnL+ → book at {cur_price:.2f}")
                        ps.close_position(pos.position_id, reason, realized_r)
                        try:
                            from execution.pending_orders import pending_store as _ps2
                            _ps2().cancel_for_position(pos.position_id)
                        except Exception:
                            pass
                        exits.append({"position_id": pos.position_id, "exit": "strong_zone_book",
                                      "realized_r": round(realized_r, 4), "reason": reason})
                        LOG.info(f"[ingest] STRONG-ZONE BOOK {pos.position_id} {pos.side} "
                                 f"R={realized_r:.2f}: {reason}")
            except Exception as e:
                LOG.warning(f"[ingest] strong-zone book check failed: {e}")

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

        # Intra-day VP refresh — rebuild current-day VP every Nth primary bar
        # so HVN/LVN, POC, VAH/VAL reflect today's accumulating volume.
        if bar.tf == primary_tf:
            refresh_n = int((settings.get("vp_cache") or {}).get("intraday_refresh_bars", 5))
            if refresh_n > 0 and (bar.close_ts // 60) % refresh_n == 0:
                try:
                    from pipeline.features.vp_cache import build_and_save as _vp_build
                    _vp_cfg = settings.get("vp_cache", {}) or {}
                    _vp_build(
                        [bar.symbol], primary_tf,
                        session_start_utc=_vp_cfg.get("session_start_utc", {}),
                        vp_bin_size=_vp_cfg.get("vp_bin_size", {}),
                        venue_price_offset=_vp_cfg.get("venue_price_offset", {}),
                        symbol_map=(settings.get("execution") or {}).get("symbol_map", {}),
                    )
                except Exception as e:
                    LOG.warning(f"[ingest] intraday VP refresh failed: {e}")

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
                    venue_price_offset=_vp_cfg.get("venue_price_offset", {}),
                    symbol_map=(settings.get("execution") or {}).get("symbol_map", {}),
                )
                # Write journal for the day that just closed. snapshot_if_boundary returns
                # a LIST of period names (["daily"], ["daily","weekly"], …) — NOT a dict.
                # (Old `snapped.get("daily")` / `snapped["daily"]` 500'd on every session
                # rollover: AttributeError on the list.) The closed-day date key is derived
                # from the PREVIOUS bar's close_ts (the day that just ended), session-anchored.
                if isinstance(snapped, (list, tuple)) and "daily" in snapped:
                    try:
                        from pipeline.features.daily_journal import write_day_journal
                        from pipeline.features.vp_cache import _session_day_key, _normalize_anchor
                        _vpc = settings.get("vp_cache", {}) or {}
                        sess_anchor = (_vpc.get("session_start_utc", {}) or {}).get(bar.symbol, 0)
                        closed_date = _session_day_key(int(prev.close_ts), _normalize_anchor(sess_anchor))
                        result = write_day_journal(bar.symbol, primary_tf, closed_date, sess_anchor)
                        if result:
                            LOG.info(f"[ingest] Daily journal written: {result.name}")
                    except Exception:
                        LOG.exception("[ingest] daily journal write failed (non-fatal)")

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

    # Tick anchor bar registry (detect high-vol/delta bars; evict stale)
    if bar.tf == primary_tf:
        try:
            from pipeline.features.anchor_bar import update as _anchor_update
            _anchor_update(bar.symbol, bar, recent_bars)
        except Exception:
            pass

    # Sweep detection per-bar — populates registry + persistent log
    if bar.tf == primary_tf:
        try:
            from pipeline.features.sweep import detect as _sweep_detect, tick_registry as _sweep_tick, log_sweep as _log_sweep
            from pipeline.features.swing import get as _swing_get
            _sp = _swing_get(bar.symbol)
            if _sp is not None:
                _sw = _sweep_detect(bar, _sp, recent_bars)
                if _sw.type != "none":
                    _log_sweep(bar.symbol, _sw, bar.close_ts, bar.tf)
            _sweep_tick(bar.symbol, bar)
        except Exception:
            pass

    # Remember the last CVD divergence per symbol — populated every bar so the value
    # is always current for any consumer (reversal_hvn filter, exits, dashboard),
    # independent of which strategies happen to scan.
    if bar.tf == primary_tf:
        try:
            from pipeline.features.cvd_candlestick import scan_divergences as _scan_div
            from pipeline.features import cvd_div_state as _cdv
            _cdv.record_from_scan(bar.symbol, _scan_div(store().recent(bar.symbol, primary_tf, 120),
                                                        lookback=3, include_live=True))
        except Exception:
            pass

    # Big-trade detection — log on bar close so dashboard can render bubbles
    if bar.tf == primary_tf:
        try:
            from pipeline.features.big_trade import detect_events as _bt_detect, log_events as _bt_log
            from pipeline.features.vp_cache import get as _vp_get
            _recent = store().recent(bar.symbol, primary_tf, 30)
            _vp = _vp_get(bar.symbol, "daily") or {}
            _bt_events = _bt_detect(bar, _recent, _vp)
            if _bt_events:
                _bt_log(_bt_events)
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).debug(f"[ingest][big_trade] skipped: {e}")

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

    # Strategy manager fan-out — manage scoped exits every primary-TF bar (same
    # cadence as the legacy cycle loop), and allow new entries only on the decide
    # TF. All reads/writes land in per-strategy stores.
    try:
        _strat_tf = str((settings.get("decide_on_bar_close") or {}).get("tf", "15m"))
        if bar.tf in (primary_tf, _strat_tf):
            from strategies.manager import get_manager
            get_manager(settings).tick(
                bar.symbol, bar.tf, bar, settings,
                allow_entry=(bar.tf == _strat_tf),
            )
    except Exception as e:
        import logging as _l
        _l.getLogger(__name__).warning(f"[ingest][strategies] tick failed: {e}")

    return jsonify({
        "ok": True,
        "bar_id": bar.bar_id,
        "symbol": bar.symbol,
        "tf": bar.tf,
        "delta": bar.delta,
        "exits": exits,
        "sl_adjustments": [{"position_id": a.position_id, "old": a.old_sl, "new": a.new_sl, "reason": a.reason} for a in sl_adjustments],
    })
