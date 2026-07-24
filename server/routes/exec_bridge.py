"""Execution-bridge HTTP API — the thin FBExecBridge EA polls/acks here.

  POST /exec/poll  {account, [symbol]}            → {ok, commands:[...]}
  POST /exec/ack   {account, results:[{id,ok,...}]} → {ok, done, failed, unknown}
  GET  /exec/queue?account=...                     → {ok, commands:[...]}  (debug)

Optional shared-secret gate: if env FB_EXEC_TOKEN is set, requests must carry
header `X-FB-Token: <token>`. This endpoint can place REAL orders via the EA, so
set the token in any networked deployment.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from execution.exec_bridge import ExecBridge, magic_for, tf_from_magic

bp = Blueprint("exec_bridge", __name__)
_EMIT_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "exec_emit.jsonl"


def _emit_audit(row: dict) -> None:
    """Append one emit decision (arm or skip) — ground truth for diagnostics."""
    try:
        _EMIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EMIT_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), **row}) + "\n")
    except Exception:
        pass  # audit must never break execution
LOG = logging.getLogger(__name__)


def _auth_ok() -> bool:
    token = os.environ.get("FB_EXEC_TOKEN")
    if not token:
        return True  # no token configured → open (local/dev)
    return request.headers.get("X-FB-Token") == token


_RECON_LAST_TS: dict = {}       # (account, broker_symbol, tf) → last venue close_ts feed-reconciled
_RECON_HEAL_AT: dict = {}       # (analysis_symbol, tf) → last wall-clock a breach heal fired (cooldown)


def _reconcile_feeds(account: str, broker_symbol: str, settings: dict) -> None:
    """Per-close Binance(analysis)-vs-Vantage(venue) discrepancy check. Called each poll, but
    only evaluates once per NEW venue 5m/15m close (dedup on close_ts). On a basis-adjusted
    breach: log (rate-limited) and — if monitor.feed_recon_heal_on_breach — re-fetch that
    candle's 1m window from Binance aggTrades (replace=True) to overwrite a corrupt bar.

    Diagnostic + heal only; never touches arming. Fully fail-open — any error is swallowed so
    the poll path is never broken by the comparator."""
    mon = (settings.get("monitor") or {})
    if not mon.get("feed_recon_enabled", True):
        return
    try:
        from pipeline.feed_reconcile import compare_close
        from pipeline.state_store import store as _store
        symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
        analysis = {v: k for k, v in symbol_map.items()}.get(broker_symbol, broker_symbol)
        tol_pct = float(mon.get("feed_recon_tol_pct", 0.0008) or 0.0008)
        tol_abs = float(mon.get("feed_recon_tol_abs", 2.0) or 2.0)
        basis_win = int(mon.get("feed_recon_basis_window", 12) or 12)
        heal_on = bool(mon.get("feed_recon_heal_on_breach", False))
        cooldown = float(mon.get("feed_recon_heal_cooldown_s", 300) or 300)

        for tf in ("5m", "15m"):
            venue_bars = ExecBridge.get_venue_bars(account, broker_symbol, tf)
            if len(venue_bars) < 4:
                continue
            v_ts = int(getattr(venue_bars[-1], "close_ts", 0) or 0)
            key = (str(account), broker_symbol, tf)
            if not v_ts or _RECON_LAST_TS.get(key) == v_ts:
                continue                      # not a new close → skip (dedup)
            _RECON_LAST_TS[key] = v_ts
            analysis_bars = _store().recent(analysis, tf, basis_win + 4)
            breach = compare_close(analysis, tf, venue_bars, analysis_bars,
                                   tol_pct, tol_abs, basis_window=basis_win)
            if not breach:
                continue
            LOG.warning(f"[feed_recon] {analysis} {tf} close_ts={breach['close_ts']} "
                        f"venue={breach['venue_c']} analysis={breach['analysis_c']} "
                        f"basis={breach['basis']:+.3f} resid={breach['resid']:+.3f} "
                        f"ret_div={breach['ret_div']:+.3f} (tol={breach['tol']:.3f}, "
                        f"signal={breach['signal']})"
                        + (" — healing" if heal_on else " — log-only"))
            if not heal_on:
                continue
            hk = (analysis, tf)
            now = time.time()
            if now - _RECON_HEAL_AT.get(hk, 0.0) < cooldown:
                continue
            _RECON_HEAL_AT[hk] = now
            _tf_secs = {"5m": 300, "15m": 900}.get(tf, 300)
            start_ms = (v_ts - _tf_secs + 60) * 1000   # first 1m of the breached candle
            end_ms = (v_ts + 1) * 1000
            feeds = {"XAUTUSDT": ("XAUUSDT", 0.1), "BTCUSDT": ("BTCUSDT", 1.0)}
            bsym, pstep = feeds.get(analysis, (analysis, 0.1))
            try:
                from binance.backfill import backfill_window
                n = backfill_window(bsym, analysis, start_ms, end_ms, tf="1m",
                                    price_step=pstep, source="binance_agg_recon",
                                    replace=True)
                LOG.info(f"[feed_recon] healed {n} 1m bars for {analysis} covering the "
                         f"breached {tf} candle {breach['close_ts']}")
            except Exception as e:
                LOG.warning(f"[feed_recon] heal fetch failed {analysis} {tf}: {e}")
    except Exception as e:
        LOG.debug(f"[feed_recon] check skipped ({broker_symbol}): {e}")


@bp.post("/exec/poll")
def exec_poll():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    if not account:
        return jsonify({"ok": False, "error": "missing account"}), 400
    # The EA reports its live Vantage quote on each poll → cache it so the emitter
    # can rebase analysis-frame plans onto the venue price at build time.
    sym = body.get("symbol")
    bid, ask = body.get("bid"), body.get("ask")
    ExecBridge.last_poll_body = dict(body)   # DEBUG: surface the EA's raw poll body
    settings_cfg = current_app.config.get("FB_SETTINGS")
    if sym and bid and ask:
        # broker min-stop distance ($) = stops_level(points)·point — floors the grid step
        # so the innermost leg clears the freeze band (no more silently-rejected legs).
        stops_dist = float(body.get("stops_pts", 0) or 0) * float(body.get("point", 0.0) or 0.0)
        ExecBridge.set_quote(account, sym, float(bid), float(ask), stops_dist=stops_dist)

    # Vantage-native closed bars (EA CopyRates) — lets a feed comparator check the
    # SAME OHLC the broker fills against, no analysis-feed rebase needed. Absent on
    # older EA binaries; consumers fall back to the analysis feed.
    if sym:
        from pipeline.types import Bar, OHLC
        for _tf, _key in (("5m", "bars_5m"), ("15m", "bars_15m")):
            _raw_bars = body.get(_key)
            if not _raw_bars:
                continue
            try:
                _bars = [Bar(bar_id=f"{sym}|{_tf}|{int(b['ts'])}|venue", symbol=sym, tf=_tf,
                             close_ts=int(b["ts"]), source="live",
                             ohlc=OHLC(o=float(b["o"]), h=float(b["h"]),
                                       l=float(b["l"]), c=float(b["c"])),
                             bid_ladder=(), ask_ladder=())
                          for b in _raw_bars]
                ExecBridge.set_venue_bars(account, sym, _tf, _bars)
            except Exception:
                LOG.exception(f"[exec] venue bars parse error ({_tf})")

        # Per-close Binance-vs-Vantage feed discrepancy check (once per new venue 5m/15m close).
        _reconcile_feeds(account, sym, settings_cfg or {})
    # Per-magic open-state + cycle monitor. The EA sends a `magics` array — one entry
    # per (strategy×TF) pool it holds — so each TF cycle is tracked and exited in
    # isolation. tf is recovered from the magic. A flatten ships in the same response
    # (saves a ~1s round-trip). Falls back to the legacy aggregate fields for an older
    # EA binary (single pool, no per-magic breakdown).
    magics = body.get("magics")
    # Stash the poll's per-magic breakdown + account/broker for the out-of-band feed-hedge
    # (feed_monitor thread only knows analysis symbols; the EA keeps polling Vantage even when
    # the Binance feed is down, so this is the fresh exposure snapshot the hedge sizes against).
    _symmap = (settings_cfg or {}).get("execution", {}).get("symbol_map") or {}
    _b2a = {v: k for k, v in _symmap.items()}
    analysis_sym = _b2a.get(sym, sym) if sym else sym
    if sym and analysis_sym and isinstance(magics, list):
        try:
            ExecBridge.set_last_magics(analysis_sym, account, sym, magics)
            from pipeline import feed_hedge as _fh
            _fh.rehydrate(analysis_sym, magics)   # adopt a hedge that survived a restart
        except Exception:
            LOG.debug("[exec] feed_hedge stash/rehydrate skipped")
    if sym and isinstance(magics, list) and magics:
        # Re-adopt any live magic orphaned/reaped across a restart BEFORE the monitor
        # loop, so its arm record exists when monitor_cycle looks it up.
        try:
            ExecBridge.reconcile_from_poll(account, sym, magics)
        except Exception:
            LOG.exception("[exec] reconcile_from_poll error")
        for m in magics:
            try:
                mg = int(m.get("magic", 0))
                tf_m = tf_from_magic(mg)
                if not tf_m:
                    continue
                b = int(m.get("buys", 0)); s = int(m.get("sells", 0))
                ExecBridge.set_open(account, sym, b + s, int(m.get("pendings", 0)), tf=tf_m, magic=mg)
                ExecBridge.monitor_cycle(account, sym, settings_cfg, tf=tf_m, magic=mg,
                                         pnl=float(m.get("pnl", 0.0)), buys=b, sells=s,
                                         buy_pnl=float(m.get("buy_pnl", 0.0)),
                                         sell_pnl=float(m.get("sell_pnl", 0.0)))
            except Exception:
                LOG.exception("[exec] per-magic cycle monitor error")  # never break the poll
    elif sym and ("positions" in body or "pendings" in body):
        # legacy single-pool path (no magics array)
        ExecBridge.set_open(account, sym, int(body.get("positions", 0)),
                            int(body.get("pendings", 0)))
        buys = int(body["buys"]) if "buys" in body else None
        sells = int(body["sells"]) if "sells" in body else None
        pnl = float(body["pnl"]) if "pnl" in body else None
        try:
            ExecBridge.monitor_cycle(account, sym, settings_cfg, pnl=pnl, buys=buys, sells=sells)
        except Exception:
            LOG.exception("[exec] cycle monitor error")  # never break the poll
    commands = ExecBridge.poll(account)
    if commands:
        LOG.info(f"[exec] poll account={account} → {len(commands)} command(s)")
    # Feed-hedge draw-state so the EA can annotate the chart (retry count / hedge side / qty /
    # entry price). {active:false, retry:N} when no hedge is open.
    hedge_draw = {"active": False, "retry": 0}
    try:
        from pipeline import feed_hedge as _fh
        if analysis_sym:
            hedge_draw = _fh.chart_state(analysis_sym)
    except Exception:
        pass
    return jsonify({"ok": True, "account": account, "commands": commands, "hedge": hedge_draw})


@bp.post("/exec/ack")
def exec_ack():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    results = body.get("results") or []
    summary = ExecBridge.ack(results)
    LOG.info(f"[exec] ack account={body.get('account')} → {summary}")
    return jsonify({"ok": True, **summary})


_FEED_STALE_LOG_AT: dict = {}   # analysis_symbol → last wall-clock we logged a stale-feed skip (rate-limit)


def _feed_is_stale(analysis_symbol: str, grid_cfg: dict) -> float | None:
    """Guard against arming on a DEAD analysis feed. Returns the age (seconds) of the newest
    REAL 1m bar when it exceeds feed_max_age_s, else None (feed fresh → arm allowed).

    Why 1m: it's the fastest series, so the tightest liveness signal — if 1m has gone quiet the
    higher-TF zones/ATR are all stale too. Sentinel/forming bars (close_ts >= 9e9) are dropped so
    a frozen placeholder can't masquerade as a fresh bar (same filter atr_from_store/put use).

    This is the exact failure that took the system down 2026-07-15: the Binance feed process died,
    bars stopped, but nothing suppressed new arms — the grid would keep arming on frozen zones.
    Existing cycles are UNAFFECTED (their exits ride the EA's venue-price poll, independent of this
    feed); only FRESH arms are frozen, which is correct — you must not draw a new grid on stale
    zones. Fail-OPEN on any error: a guard hiccup must never itself freeze arming.
    Returns the age so the caller can log it once (rate-limited)."""
    try:
        max_age = float(grid_cfg.get("feed_max_age_s", 180) or 180)
        from pipeline.state_store import store
        bars = [b for b in store().recent(analysis_symbol, "1m", 4)
                if getattr(b, "close_ts", 0) and b.close_ts < 9_000_000_000]
        if not bars:
            return 1e9   # no real bars at all → treat as maximally stale
        age = time.time() - float(bars[-1].close_ts)
        return age if age > max_age else None
    except Exception as e:
        LOG.warning(f"[feed_stale] check failed for {analysis_symbol}: {e} — failing open (allow arm)")
        return None


def _feed_stale_skip(analysis_symbol: str, broker_symbol: str, tf: str, grid_cfg: dict) -> bool:
    """True if the analysis feed is stale (caller should skip the arm). Logs at most once every
    30s per symbol so a ~1s poll loop doesn't flood the log."""
    age = _feed_is_stale(analysis_symbol, grid_cfg)
    if age is None:
        return False
    now = time.time()
    if now - _FEED_STALE_LOG_AT.get(analysis_symbol, 0.0) >= 30.0:
        _FEED_STALE_LOG_AT[analysis_symbol] = now
        LOG.warning(f"[feed_stale] {analysis_symbol} last 1m bar {age:.0f}s old "
                    f"(>{float(grid_cfg.get('feed_max_age_s', 180) or 180):.0f}s) — "
                    f"arming suspended ({broker_symbol}/{tf})")
    return True


@bp.post("/exec/emit_grid")
def exec_emit_grid():
    """Build the neutral grid for `symbol`/`tf`, rebase onto the account's cached
    venue quote, and enqueue it as PLACE_PENDING commands the EA will drain.

    Body: {account, symbol(broker or analysis), tf, [trigger_hint], [close_first]}.
    Requires the EA to have polled at least once (so a venue quote is cached).
    Returns verdict=skip with the planner's reason when no grid arms.
    """
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]
    tf = body.get("tf") or settings["instrument"]["primary_tf"]
    # "structural" = {hvn_inside_touch, vp_level_touch} — arm the best of HVN-edge or
    # VP-level (POC/VAH/VAL/naked-POC/LVN) touch. May also be one kind or a comma list.
    trigger_hint = str(body.get("trigger_hint") or "structural")
    close_first = bool(body.get("close_first", True))
    if not account:
        return jsonify({"ok": False, "error": "missing account"}), 400

    # broker↔analysis symbol resolution (same map as /grid_levels)
    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    symbol = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(symbol, req_symbol)

    # Feed-liveness gate: suppress FRESH arms if the analysis feed has gone stale (dead feed →
    # frozen zones — the 2026-07-15 outage). Existing cycles unaffected (venue-price driven).
    grid_cfg = settings.get("grid_levels") or {}
    if _feed_stale_skip(symbol, broker_symbol, tf, grid_cfg):
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": "feed_stale",
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # venue quote the EA last reported (the rebase target)
    quote = ExecBridge.get_quote(account, broker_symbol)
    if not quote:
        return jsonify({"ok": False, "verdict": "skip",
                        "skip_reason": f"no venue quote cached for {account}/{broker_symbol} "
                                       f"(EA must poll first)"}), 409

    from pipeline.state_store import store
    from execution.grid_planner import plan_grid_levels, _hint_set
    latest = store().latest(symbol, tf)
    if latest is None:
        return jsonify({"ok": False, "verdict": "skip",
                        "skip_reason": f"no bars stored for {symbol} {tf}"}), 404

    # Freeze-aware step floor: clear the broker's min-stop distance (×1.5 margin) so the
    # innermost leg can't land inside the freeze band and get silently rejected.
    min_step_venue = float(quote.get("stops_dist", 0.0) or 0.0) * 1.5
    plan = plan_grid_levels(symbol, tf, float(latest.ohlc.c),
                            trigger_hint=trigger_hint, settings=settings,
                            venue_price=float(quote["mid"]), min_step_venue=min_step_venue)
    # Cycle state is keyed by MAGIC (strategy×TF) so independent setups (hvn / squeeze /
    # vp …) run as parallel cycles on the SAME symbol+TF. Compute it once from the plan's
    # trigger; "" trigger (skip) → 0 (the legacy single pool). The vp_levels SETUP (va OR
    # vp_level_touch) arms under one dedicated setup magic so the report reads it as one.
    if trigger_hint == "vp_levels" and plan.trigger_kind:
        leg_magic = magic_for("vp_levels", tf)
    else:
        leg_magic = magic_for(plan.trigger_kind, tf) if plan.trigger_kind else 0
    if plan.verdict != "arm":
        # episode ended → next arm on this magic is a fresh touch
        ExecBridge.clear_emit(account, symbol, magic=leg_magic)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": plan.skip_reason})
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": plan.skip_reason,
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Strict: plan_grid_levels falls back to ALL triggers when the hinted one is
    # absent — but emit must fire ONLY a requested-group strategy, never a stray
    # hvn_edge/va/imbalance grid. Membership (not equality) so the group works.
    hint_set = _hint_set(trigger_hint)
    if hint_set and plan.trigger_kind not in hint_set:
        ExecBridge.clear_emit(account, symbol, magic=leg_magic)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": f"trigger_mismatch:{plan.trigger_kind}"})
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": f"no_{trigger_hint}_trigger (got {plan.trigger_kind})",
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Cycle-ownership gate: now ONE active cycle PER (account, broker_symbol, tf) —
    # each TF runs an independent parallel cycle, isolated by its own magic
    # (magic_for(kind, tf)), so the EA can attribute every fill to the right TF pool.
    # This TF's cycle, while it holds a live position, can't be re-armed (would
    # flatten its own fills); it may refresh only while flat. Sibling TFs are not
    # consulted here — they own separate pools.
    arm = ExecBridge.get_last_arm(account, broker_symbol, magic=leg_magic) or {}
    open_state = ExecBridge.get_open(account, broker_symbol, magic=leg_magic)
    force = bool(body.get("force", False))
    if not force and arm.get("active") and open_state.get("positions", 0) > 0:
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": "position_open",
                        "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf,
                        "open": open_state})
        # active+flat, or inactive → fall through and (re)arm this TF's straddle

    # Fulcrum dedup: ONE grid per touched-level episode. Skip if the fulcrum hasn't
    # moved beyond tol since the last arm (prevents re-placing the identical straddle
    # every bar while price camps on a level). clear_emit (called on every skip above)
    # resets it, so a moved fulcrum re-arms. mark_emit set after a successful arm.
    dedup_pct = float((settings.get("grid_levels") or {}).get("emit_dedup_pct", 0.0007) or 0.0)
    dedup_tol = float(quote["mid"]) * dedup_pct
    if not force and not ExecBridge.should_emit(account, symbol, plan.fulcrum, dedup_tol, magic=leg_magic):
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": "dedup:same_fulcrum", "fulcrum": plan.fulcrum})
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": "dedup:same_fulcrum",
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Re-arm clears stale pendings with CANCEL_PENDINGS (never CLOSE_ALL) so it can't
    # flatten a live position. The deliberate flatten is owned solely by monitor_cycle.
    # leg_magic (strategy × TF, computed above) identifies every leg in MT5 history AND
    # keys the per-(strategy×TF) cycle so independent setups run in parallel on one symbol.
    # Leg-TP policy: net_profit_exit_only places legs WITHOUT a per-order TP so no single
    # side self-closes while the other dangles (basket net_target owns the exit). BUT
    # leg_tp_ceiling re-adds the computed structural TP as a FAR ceiling (visible target +
    # runaway-move backstop); the basket exit stays primary since it's closer.
    grid_cfg_ep = (settings.get("grid_levels") or {})
    leg_tp = (not bool(grid_cfg_ep.get("net_profit_exit_only", False))
              or bool(grid_cfg_ep.get("leg_tp_ceiling", False)))
    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan,
                                        close_first=close_first, clear_kind="cancel",
                                        magic=leg_magic, leg_tp=leg_tp, tf=tf)
    edge = plan.trigger_context.get("edge", "")
    net_target = float(((settings.get("grid_levels") or {}).get("cycle_net_target_usd", 0.0)) or 0.0)
    # ground truth + cycle state: touched edge (=fulcrum), TF owner, structural targets
    # (tp_up=buy target, tp_down=sell target), and the exit-monitor bookkeeping fields.
    # node bounds (the HVN/LVN the fulcrum sits on) — rebased to the venue frame like
    # the legs, so the EA dashboard reports the price band the broker actually quotes.
    # 2026-07-22: ADDITIVE shift, matching _rebase_to_venue (b93af34) and
    # vp_cache._shift_vp. This was a multiplicative ratio, which left the node bounds
    # in a different frame from the legs/fulcrum they describe — the same
    # additive-vs-multiplicative mismatch that put the fulcrum off the drawn edge.
    _shift = (plan.venue_anchor - plan.analysis_anchor) if plan.analysis_anchor else 0.0
    _nl = float(plan.trigger_context.get("node_low", 0.0) or 0.0)
    _nh = float(plan.trigger_context.get("node_high", 0.0) or 0.0)
    node_low = (_nl + _shift) if _nl else 0.0
    node_high = (_nh + _shift) if _nh else 0.0
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=edge,
                            trigger_kind=plan.trigger_kind, venue_mid=quote["mid"], magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            bias_peak=0.0, bias_booked=False,
                            pending_retry=[],
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            net_target_usd=net_target, max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank,
                            # Frame anchors — every price above is VENUE frame (post
                            # _rebase_to_venue). Persist both ends so a later consumer can
                            # convert back to the ANALYSIS frame that bars and a freshly
                            # computed VP live in. Without these the shift silently reads
                            # 0.0 and the two frames get compared as if they were one.
                            analysis_anchor=plan.analysis_anchor,
                            venue_anchor=plan.venue_anchor)
    ExecBridge.mark_emit(account, symbol, plan.fulcrum, magic=leg_magic)   # dedup: this fulcrum is now armed
    _emit_audit({"account": account, "symbol": symbol, "broker_symbol": broker_symbol,
                 "tf": tf, "verdict": "arm", "trigger_kind": plan.trigger_kind, "edge": edge,
                 "fulcrum": plan.fulcrum, "venue_mid": quote["mid"],
                 "analysis_anchor": plan.analysis_anchor, "n_per_side": plan.n_per_side,
                 "step": plan.step,
                 "buy_legs": [l.price for l in plan.buy_legs],
                 "sell_legs": [l.price for l in plan.sell_legs],
                 "buy_tp": plan.buy_tp, "sell_tp": plan.sell_tp,
                 "squeeze_ok": plan.squeeze_ok, "squeeze_rank": plan.squeeze_rank})
    LOG.info(f"[exec] emit_grid {account} {broker_symbol} {tf} → armed [{plan.trigger_kind} "
             f"edge={edge} fulcrum={plan.fulcrum}], {len(cmds)} command(s)")
    return jsonify({
        "ok": True, "verdict": "arm", "symbol": symbol, "broker_symbol": broker_symbol,
        "venue_mid": quote["mid"], "fulcrum": plan.fulcrum, "n_per_side": plan.n_per_side,
        "buy_legs": [{"price": l.price, "lot": l.lot} for l in plan.buy_legs],
        "sell_legs": [{"price": l.price, "lot": l.lot} for l in plan.sell_legs],
        "buy_tp": plan.buy_tp, "sell_tp": plan.sell_tp, "commands_enqueued": len(cmds),
    })


@bp.post("/exec/test_order")
def exec_test_order():
    """Forced round-trip test — bypasses all strategy/arm logic.

    Default: enqueue ONE tiny pending stop a safe distance from market (won't fill
    → no risk), so you can confirm the EA delivers→places→acks. Then call again
    with {"close_only": true} to CLOSE_ALL (cancels it). Proves the bridge round
    trip on a DEMO account before trusting the arm logic.

    Body: {account, symbol(broker or analysis), [side=buy|sell],
           [offset_pct=0.005], [lot=0.01], [close_only=false]}.
    Requires the EA to have polled once (cached venue quote).
    """
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    from execution.exec_bridge import PLACE_PENDING, CLOSE_ALL
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    if not account:
        return jsonify({"ok": False, "error": "missing account"}), 400

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]
    analysis = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(analysis, req_symbol)

    if bool(body.get("close_only", False)):
        cmd = ExecBridge.enqueue(account, CLOSE_ALL, broker_symbol)
        LOG.info(f"[exec] test_order CLOSE_ALL {account} {broker_symbol}")
        return jsonify({"ok": True, "action": "close_all", "broker_symbol": broker_symbol,
                        "command_id": cmd.id})

    quote = ExecBridge.get_quote(account, broker_symbol)
    if not quote:
        return jsonify({"ok": False, "error": f"no venue quote cached for "
                        f"{account}/{broker_symbol} (EA must poll first)"}), 409

    side = str(body.get("side") or "buy").lower()
    offset_pct = float(body.get("offset_pct", 0.005))   # 0.5% away → stays pending
    lot = float(body.get("lot", 0.01))
    if side == "buy":
        order_type, price = "buy_stop", quote["ask"] * (1.0 + offset_pct)
    else:
        order_type, price = "sell_stop", quote["bid"] * (1.0 - offset_pct)

    cmd = ExecBridge.enqueue(account, PLACE_PENDING, broker_symbol,
                             order_type=order_type, price=price, lot=lot,
                             sl=0.0, tp=0.0, comment="FB|test")
    LOG.info(f"[exec] test_order {account} {broker_symbol} {order_type} @ {price:.5f} lot {lot}")
    return jsonify({
        "ok": True, "action": "place_pending", "broker_symbol": broker_symbol,
        "order_type": order_type, "price": round(price, 5), "lot": lot,
        "venue_mid": quote["mid"], "command_id": cmd.id,
        "note": "stays pending (away from market). Call with close_only:true to cancel.",
    })


@bp.post("/exec/zones")
def exec_zones():
    """HVN/LVN zones for the EA to draw. `zones` is the cached, session-anchored
    DAILY VP (vp_cache.get) — the SAME source the dashboard's VolumeProfile panel
    renders, for visual parity across the whole session.

    `zones_session`, when `tf` is given, is the SAME session-blended HVN source
    hvn_inside_touch actually arms on (zone_triggers._session_hvn_zones — rolling
    intrasession VP during NY/London/Overlap, cached daily during Asia/Off, plus the
    prior-day vacuum-fallback node). During NY/London/Overlap this can differ from
    `zones` since rolling VP reacts faster than the cached daily profile; draw it so
    the touched edge on-screen matches the edge the emitter actually straddles.

    Body: {account, symbol(broker or analysis), [tf]}. `tf` (e.g. "5m"/"15m") is
    required for `zones_session`; omitted → zones_session is empty.
    Returns {ok, zones:[{kind, lo, hi}], zones_session:[{kind, lo, hi}], venue_mid, fulcrum}.
    """
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    symbol = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(symbol, req_symbol)

    # Same call as the dashboard (server/routes/dashboard.py): cached daily VP with
    # the additive venue offset already applied → zones are in the venue frame.
    from pipeline.features import vp_cache
    daily = vp_cache.get(symbol, "daily") or {}
    zones = []
    for z in (daily.get("hvn_zones") or []):
        zones.append({"kind": "hvn", "lo": round(float(z["low"]), 5),
                      "hi": round(float(z["high"]), 5)})
    for z in (daily.get("lvn_zones") or []):
        zones.append({"kind": "lvn", "lo": round(float(z["low"]), 5),
                      "hi": round(float(z["high"]), 5)})

    # VP point-levels the grid actually TRIGGERS on (vp_level_touch fulcrums), drawn as
    # labeled lines — only the levels enabled in grid_levels.vp_fulcrum_levels. Same
    # venue-shifted daily VP, so they line up with the zones above and the dashboard.
    enabled = set((settings.get("grid_levels") or {}).get("vp_fulcrum_levels", []) or [])
    levels = []
    for k in ("poc", "vah", "val", "naked_poc"):
        if k in enabled:
            v = daily.get(k)
            if isinstance(v, (int, float)) and v > 0:
                levels.append({"kind": k, "price": round(float(v), 5)})

    # Computed volume-at-price histogram (venue-shifted) for the EA to draw as a sideways
    # profile. Rebuilt from bars (cache keeps only aggregates). Same daily window as zones.
    prof = vp_cache.period_profile(symbol, "daily") or {}
    profile = prof.get("profile", [])
    vp_bin = prof.get("bin", 0.0)

    quote = ExecBridge.get_quote(account, broker_symbol) or {}

    # Session-blended HVN zones — the SAME source hvn_inside_touch arms on
    # (zone_triggers._session_hvn_zones), rebased analysis→venue the same way
    # plan_grid_levels rebases the emitted grid (ratio = venue_mid / analysis_close).
    # Only computed when the EA supplies a tf, since the source is per-TF.
    zones_session = []
    zone_tf_req = str(body.get("tf") or "")
    if zone_tf_req:
        try:
            from pipeline.state_store import store
            from execution.zone_triggers import _session_hvn_zones
            bars = store().recent(symbol, zone_tf_req, 101)
            if bars:
                sess_zones, _sess = _session_hvn_zones(symbol, zone_tf_req, bars)
                analysis_anchor = float(bars[-1].ohlc.c)
                venue_mid = float(quote.get("mid") or 0.0)
                ratio = (venue_mid / analysis_anchor
                         if analysis_anchor > 0 and venue_mid > 0 else 1.0)
                zones_session = [
                    {"kind": "hvn", "lo": round(lo * ratio, 5), "hi": round(hi * ratio, 5)}
                    for lo, hi in sess_zones
                ]
        except Exception:
            zones_session = []
    # dashboard shows the cycle for the EA's drawn TF (body.tf). Cycles are now per-TF,
    # so without a tf we'd find nothing — default to the zone TF the EA reports.
    zone_tf = str(body.get("tf") or "")
    arm = ExecBridge.get_active_arm_for_tf(account, broker_symbol, zone_tf) or {}

    # ict_fvg paper-strategy overlay (entry/SL/TP, fib zone, FVGs, ChoCh) — published in
    # the ANALYSIS frame; rebase onto the venue (ratio = venue_mid / analysis_anchor) so
    # the EA can draw why it triggered. Dropped if no quote / stale (>12h).
    ict_out = None
    ov = ExecBridge.get_ict_overlay(symbol)
    mid = float(quote.get("mid") or 0.0)
    if ov and mid > 0 and float(ov.get("anchor") or 0.0) > 0:
        fresh = (not ov.get("ts")) or (time.time() - float(ov["ts"]) <= 12 * 3600)
        if fresh:
            ratio = mid / float(ov["anchor"])
            pk = ("entry", "sl", "tp", "fib_lo", "fib_hi", "fvg_low", "fvg_high",
                  "htf_fvg_low", "htf_fvg_high", "choch_level")
            ict_out = {k: round(float(ov[k]) * ratio, 5) for k in pk if ov.get(k)}
            ict_out["side"] = ov.get("side", "")
            ict_out["status"] = ov.get("status", "")

    return jsonify({"ok": True, "zones": zones, "zones_session": zones_session,
                    "levels": levels, "ict": ict_out,
                    "profile": profile, "vp_bin": vp_bin,
                    "venue_mid": quote.get("mid", 0.0),
                    "symbol": symbol, "broker_symbol": broker_symbol,
                    "fulcrum": arm.get("fulcrum", 0.0), "emit_tf": arm.get("tf", ""),
                    "emit_edge": arm.get("edge", ""),
                    "trigger_kind": arm.get("trigger_kind", ""),
                    "node_low": arm.get("node_low", 0.0),
                    "node_high": arm.get("node_high", 0.0)})


@bp.get("/exec/queue")
def exec_queue():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    account = request.args.get("account")
    return jsonify({"ok": True, "commands": ExecBridge.snapshot(account),
                    "last_poll_body": getattr(ExecBridge, "last_poll_body", None)})
