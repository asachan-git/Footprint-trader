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
    if sym and bid and ask:
        # broker min-stop distance ($) = stops_level(points)·point — floors the grid step
        # so the innermost leg clears the freeze band (no more silently-rejected legs).
        stops_dist = float(body.get("stops_pts", 0) or 0) * float(body.get("point", 0.0) or 0.0)
        ExecBridge.set_quote(account, sym, float(bid), float(ask), stops_dist=stops_dist)
    # Per-magic open-state + cycle monitor. The EA sends a `magics` array — one entry
    # per (strategy×TF) pool it holds — so each TF cycle is tracked and exited in
    # isolation. tf is recovered from the magic. A flatten ships in the same response
    # (saves a ~1s round-trip). Falls back to the legacy aggregate fields for an older
    # EA binary (single pool, no per-magic breakdown).
    settings_cfg = current_app.config.get("FB_SETTINGS")
    magics = body.get("magics")
    if sym and isinstance(magics, list) and magics:
        grid_cfg = ((settings_cfg or {}).get("grid_levels") or {})
        for m in magics:
            try:
                mg = int(m.get("magic", 0))
                tf_m = tf_from_magic(mg)
                if not tf_m:
                    continue
                b = int(m.get("buys", 0)); s = int(m.get("sells", 0))
                pend = int(m.get("pendings", 0))
                buy_pnl  = float(m.get("buy_pnl",  0.0))
                sell_pnl = float(m.get("sell_pnl", 0.0))
                ExecBridge.set_open(account, sym, b + s, pend, tf=tf_m, magic=mg)

                # Stateless trail SL — fires purely from live poll data, no arm state
                # needed. EA's "only improve" guard acts as the ratchet: it will never
                # widen a stop already set, so sending locked_pnl every poll is safe.
                # When P&L is at peak the SL tightens; when P&L retraces the server
                # sends a lower value and the EA silently skips it.
                if bool(grid_cfg.get("bias_trail_enabled", True)) and (b + s) > 0:
                    combined = buy_pnl + sell_pnl
                    _act_by_tf = grid_cfg.get("bias_trail_activate_by_tf") or {}
                    activate   = float(_act_by_tf.get(tf_m) or grid_cfg.get("bias_trail_activate_usd", 5.0) or 0.0)
                    if activate > 0 and combined >= activate:
                        bias  = "buy"  if (b > 0 and buy_pnl  > 0 and buy_pnl  >= sell_pnl) else \
                                "sell" if (s > 0 and sell_pnl > 0) else ""
                        hedge = ("sell" if bias == "buy" else "buy") if bias else ""
                        _gb_bias  = grid_cfg.get("bias_trail_giveback_pct_by_tf") or {}
                        _gb_hedge = grid_cfg.get("hedge_trail_giveback_pct_by_tf") or {}
                        gb_bias   = float(_gb_bias.get(tf_m)  or grid_cfg.get("bias_trail_giveback_pct",  40.0) or 0.0)
                        gb_hedge  = float(_gb_hedge.get(tf_m) or grid_cfg.get("hedge_trail_giveback_pct", 40.0) or 0.0)
                        if bias:
                            side_pnl = buy_pnl if bias == "buy" else sell_pnl
                            if side_pnl > 0:
                                ExecBridge.enqueue(account, "MODIFY_SL", sym, magic=mg, side=bias,
                                                   locked_pnl=side_pnl * (1.0 - gb_bias / 100.0),
                                                   comment=f"FB|tsl|{tf_m}|{bias}|bias")
                        if hedge:
                            hedge_pnl = sell_pnl if bias == "buy" else buy_pnl
                            hedge_n   = s        if bias == "buy" else b
                            if hedge_n > 0 and hedge_pnl > 0:
                                ExecBridge.enqueue(account, "MODIFY_SL", sym, magic=mg, side=hedge,
                                                   locked_pnl=hedge_pnl * (1.0 - gb_hedge / 100.0),
                                                   comment=f"FB|tsl|{tf_m}|{hedge}|hedge")

                # Reconcile: synthesise arm state for exit logic (net_target / full_hedge /
                # one_sided_remnant) if positions exist but no arm state (post-restart).
                if (b + s > 0) and not ExecBridge.get_last_arm(account, sym, magic=mg):
                    ExecBridge.set_last_arm(account, sym, tf=tf_m, magic=mg,
                                            active=True, max_pos_seen=b + s, pend_seen=pend,
                                            n_per_side=0, tp_up=0.0, tp_down=0.0,
                                            cycle_trail_peak=0.0, flatten_ts=0.0)
                    LOG.info(f"[reconcile] {sym} magic={mg} tf={tf_m} synthesised arm "
                             f"from live positions (buys={b} sells={s})")

                ExecBridge.monitor_cycle(account, sym, settings_cfg, tf=tf_m, magic=mg,
                                         pnl=float(m.get("pnl", 0.0)), buys=b, sells=s,
                                         buy_pnl=buy_pnl, sell_pnl=sell_pnl)
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
    return jsonify({"ok": True, "account": account, "commands": commands})


@bp.post("/exec/ack")
def exec_ack():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    results = body.get("results") or []
    summary = ExecBridge.ack(results)
    LOG.info(f"[exec] ack account={body.get('account')} → {summary}")
    return jsonify({"ok": True, **summary})


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

    from pipeline.state_store import store
    from execution.grid_planner import plan_grid_levels, _hint_set
    latest = store().latest(symbol, tf)
    if latest is None:
        return jsonify({"ok": False, "verdict": "skip",
                        "skip_reason": f"no bars stored for {symbol} {tf}"}), 404

    # venue quote the EA last reported (the rebase target).
    # Fallback: use the latest bar close as mid when the server just restarted and the
    # EA hasn't polled yet. stops_dist=0 is safe — the EA-side freeze guard still fires.
    quote = ExecBridge.get_quote(account, broker_symbol)
    if not quote:
        bar_mid = float(latest.ohlc.c)
        quote = {"bid": bar_mid, "ask": bar_mid, "mid": bar_mid, "stops_dist": 0.0}
        LOG.warning(f"[emit_grid] no EA quote for {account}/{broker_symbol} — "
                    f"using bar close {bar_mid:.5f} as mid fallback")

    # Freeze-aware step floor: clear the broker's min-stop distance (×1.5 margin) so the
    # innermost leg can't land inside the freeze band and get silently rejected.
    min_step_venue = float(quote.get("stops_dist", 0.0) or 0.0) * 1.5
    # hvn_edge tick-mode: use live venue quote as current_price so detection + ladder
    # checks both operate on the actual market price, not a potentially-stale bar close.
    # For bar-close hints (squeeze, vp_levels) keep bar close as the analysis anchor.
    analysis_price = (float(quote["mid"]) if trigger_hint == "hvn_edge"
                      else float(latest.ohlc.c))
    plan = plan_grid_levels(symbol, tf, analysis_price,
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
    # Trail-SL guard: if the trail has already set a broker-side SL for this cycle
    # (bias_peak >= activate), don't re-arm with fresh pending orders — the existing
    # positions are protected and new legs would open unprotected exposure.
    if not force and arm.get("active"):
        _grid_cfg = (settings.get("grid_levels") or {})
        _act_by_tf = _grid_cfg.get("bias_trail_activate_by_tf") or {}
        _activate = float(_act_by_tf.get(tf) or _grid_cfg.get("bias_trail_activate_usd") or 0.0)
        _peak = float(arm.get("bias_peak") or 0.0)
        if _activate > 0 and _peak >= _activate:
            return jsonify({"ok": True, "verdict": "skip", "skip_reason": "trail_sl_active",
                            "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf,
                            "bias_peak": _peak, "activate": _activate})
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
                                        magic=leg_magic, leg_tp=leg_tp)
    edge = plan.trigger_context.get("edge", "")
    net_target = float(((settings.get("grid_levels") or {}).get("cycle_net_target_usd", 0.0)) or 0.0)
    # ground truth + cycle state: touched edge (=fulcrum), TF owner, structural targets
    # (tp_up=buy target, tp_down=sell target), and the exit-monitor bookkeeping fields.
    # node bounds (the HVN/LVN the fulcrum sits on) — rebased to the venue frame like
    # the legs, so the EA dashboard reports the price band the broker actually quotes.
    _ratio = (plan.venue_anchor / plan.analysis_anchor) if plan.analysis_anchor else 1.0
    node_low = float(plan.trigger_context.get("node_low", 0.0) or 0.0) * _ratio
    node_high = float(plan.trigger_context.get("node_high", 0.0) or 0.0) * _ratio
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=edge,
                            trigger_kind=plan.trigger_kind, venue_mid=quote["mid"], magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            bias_peak=0.0, bias_booked=False,
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            net_target_usd=net_target, max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank)
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
    """HVN/LVN zones for the EA to draw — the SAME source the dashboard renders:
    the cached, session-anchored DAILY VP (vp_cache.get), already venue-shifted by
    the configured additive offset. This deliberately matches the dashboard's
    VolumeProfile panel, NOT the rolling-window VP the grid trigger uses (so the
    drawn zones are for visual parity; the armed fulcrum may sit on a session-rolling
    edge that differs slightly).

    Body: {account, symbol(broker or analysis), [tf]}. `tf` is accepted but ignored —
    daily VP is one period. Returns {ok, zones:[{kind, lo, hi}], venue_mid, fulcrum}.
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

    # Computed volume-at-price histograms: prev-D + today, smoothed, venue-shifted.
    # Each entry has {vp_bin, profile, start_ts} so the EA anchors at session open.
    _sess_profs = vp_cache.period_profiles_session(symbol)
    profiles = []
    for _sp in _sess_profs:
        _pb = _sp.get("bin", 0.0)
        _pp = _sp.get("profile", [])
        if _pb > 0 and _pp:
            profiles.append({"vp_bin": round(float(_pb), 5), "profile": _pp,
                             "start_ts": _sp.get("start_ts", 0)})
    _latest = profiles[-1] if profiles else {}
    profile = _latest.get("profile", [])
    vp_bin = _latest.get("vp_bin", 0.0)

    quote = ExecBridge.get_quote(account, broker_symbol) or {}
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

    # Active cycles for the EA dashboard: grid_cycles (one row per cycle) +
    # hvn_cycles (cycles mapped to their HVN node lo/hi for zone grouping).
    grid_cfg = settings.get("grid_levels") or {}
    by_tf = grid_cfg.get("cycle_net_target_by_tf") or {}
    act_by_tf = grid_cfg.get("bias_trail_activate_by_tf") or {}
    base_target = float(grid_cfg.get("cycle_net_target_usd", 0.0) or 0.0)
    trail_fallback = float(grid_cfg.get("bias_trail_activate_usd", 5.0) or 5.0)

    cycles_detail = ExecBridge.active_cycles_detail(account, broker_symbol)
    grid_cycles, hvn_cycles = [], []
    for c in cycles_detail:
        tf = c.get("tf", "")
        net = float((by_tf.get(tf) if isinstance(by_tf, dict) else None) or base_target)
        trail = float((act_by_tf.get(tf) if isinstance(act_by_tf, dict) else None) or trail_fallback)
        grid_cycles.append({**c, "net_target": net, "trail_activate": trail})
        lo, hi = float(c.get("node_low") or 0.0), float(c.get("node_high") or 0.0)
        if lo > 0 and hi > 0:
            hvn_cycles.append({"lo": lo, "hi": hi, "magic": c["magic"],
                               "tf": tf, "edge": c.get("edge", ""),
                               "trigger_kind": c.get("trigger_kind", "")})

    return jsonify({"ok": True, "zones": zones, "levels": levels, "ict": ict_out,
                    "profile": profile, "vp_bin": vp_bin,
                    "venue_mid": quote.get("mid", 0.0),
                    "symbol": symbol, "broker_symbol": broker_symbol,
                    "fulcrum": arm.get("fulcrum", 0.0), "emit_tf": arm.get("tf", ""),
                    "emit_edge": arm.get("edge", ""),
                    "trigger_kind": arm.get("trigger_kind", ""),
                    "node_low": arm.get("node_low", 0.0),
                    "node_high": arm.get("node_high", 0.0),
                    "grid_cycles": grid_cycles, "hvn_cycles": hvn_cycles})


@bp.get("/exec/queue")
def exec_queue():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    account = request.args.get("account")
    return jsonify({"ok": True, "commands": ExecBridge.snapshot(account),
                    "last_poll_body": getattr(ExecBridge, "last_poll_body", None)})


@bp.route("/exec/tp_refresh", methods=["POST"])
def tp_refresh():
    """Recompute HVN TP targets for all active cycles and enqueue MODIFY_TP if
    either target shifted by more than `min_shift` points. Called every 120s by
    the emitter so pending orders + open position TPs track the rolling VP."""
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]
    min_shift = float(body.get("min_shift", 1.0) or 1.0)

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    analysis_sym = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(analysis_sym, req_symbol)

    from execution import zone_triggers
    from pipeline.features import vp_cache

    quote = ExecBridge.get_quote(account, broker_symbol) or {}
    mid = float(quote.get("mid") or 0.0)

    refreshed, skipped = 0, 0
    with ExecBridge._lock:
        cycles = {
            (acc, sym, mg): m
            for (acc, sym, mg), m in ExecBridge._last_arm.items()
            if acc == str(account) and sym == broker_symbol and m.get("active")
        }

    for (acc, sym, mg), cyc in cycles.items():
        old_tp_up   = float(cyc.get("tp_up")   or 0.0)
        old_tp_down = float(cyc.get("tp_down") or 0.0)
        if old_tp_up == 0.0 and old_tp_down == 0.0:
            skipped += 1
            continue

        # Recompute from fresh VP using current mid as the probe price
        probe = mid if mid > 0 else float(cyc.get("fulcrum") or 0.0)
        t = zone_triggers._t_hvn_edge(analysis_sym, probe)
        new_tp_up   = float((t.context.get("tp_up")   if t else None) or 0.0)
        new_tp_down = float((t.context.get("tp_down") if t else None) or 0.0)

        # Fall back to session zones if hvn_edge didn't yield useful targets
        if new_tp_up == 0.0 and new_tp_down == 0.0:
            from pipeline.state_store import store as _store
            from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
            tf = str(cyc.get("tf") or "15m")
            win = zone_triggers._VP_WIN.get(tf, 96)
            bars = _store().recent(analysis_sym, tf, win + 5)
            if len(bars) >= 2:
                zones, _ = zone_triggers._session_hvn_zones(analysis_sym, tf, bars)
                fulcrum = float(cyc.get("fulcrum") or probe)
                tops = [hi for lo, hi in zones if hi > fulcrum]
                bots = [lo for lo, hi in zones if lo < fulcrum]
                new_tp_up   = min(tops) if tops else 0.0
                new_tp_down = max(bots) if bots else 0.0

        shift_up   = abs(new_tp_up   - old_tp_up)   if new_tp_up   > 0 else 0.0
        shift_down = abs(new_tp_down - old_tp_down) if new_tp_down > 0 else 0.0
        if shift_up < min_shift and shift_down < min_shift:
            skipped += 1
            continue

        # Rebase from analysis to venue frame
        ratio = (float(quote.get("mid") or 0.0) / float(cyc.get("venue_mid") or 1.0)) if cyc.get("venue_mid") else 1.0
        ratio = ratio if 0.9 < ratio < 1.1 else 1.0
        buy_tp_venue  = round(new_tp_up   * ratio, 4) if new_tp_up   > 0 else 0.0
        sell_tp_venue = round(new_tp_down * ratio, 4) if new_tp_down > 0 else 0.0

        ExecBridge.enqueue(account, "MODIFY_TP", broker_symbol, magic=int(mg),
                           buy_tp=buy_tp_venue, sell_tp=sell_tp_venue)
        with ExecBridge._lock:
            cyc2 = ExecBridge._last_arm.get((acc, sym, mg)) or {}
            if cyc2:
                cyc2["tp_up"]   = new_tp_up
                cyc2["tp_down"] = new_tp_down
        LOG.info(f"[tp_refresh] {broker_symbol} magic={mg} "
                 f"tp_up {old_tp_up:.2f}→{new_tp_up:.2f} "
                 f"tp_down {old_tp_down:.2f}→{new_tp_down:.2f}")
        refreshed += 1

    return jsonify({"ok": True, "refreshed": refreshed, "skipped": skipped})
