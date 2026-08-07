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


# Skip reasons NOT worth an audit row (2026-08-06, user). These are the routine "nothing to
# do this bar" outcomes that fire on every emit call across every TF+hint — 861 skips vs 40
# arms in 24h, ~95% of the file, which buries the rows that actually matter. Matched on the
# prefix BEFORE the first ':' so parameterised reasons ("trigger_mismatch:imbalance",
# "dedup:same_fulcrum") collapse to one key. Overridable via grid_levels.audit_suppress_skips
# — set it to [] to log everything again. ARM and EXIT rows are never suppressed.
_AUDIT_SUPPRESS_DEFAULT = ("trigger_mismatch", "owned_by_touch_arm", "dedup")


def _venue_offset_for(analysis_symbol: str) -> float:
    """Additive (venue − analysis) basis, for shifting a payload out to venue frame."""
    try:
        from pipeline.features.vp_cache import venue_offset
        return float(venue_offset(analysis_symbol) or 0.0)
    except Exception:
        return 0.0


def _audit_suppressed(row: dict) -> bool:
    if row.get("verdict") != "skip":
        return False        # only ever suppress skips — arms/exits always logged
    reason = str(row.get("skip_reason") or "").split(":", 1)[0]
    if not reason:
        return False
    supp = _AUDIT_SUPPRESS_DEFAULT
    try:
        # Read from the live app config rather than threading `settings` through all 8
        # call sites. Outside a request context this raises and we fall back to the default.
        cfg = (current_app.config.get("FB_SETTINGS") or {}).get("grid_levels") or {}
        supp = cfg.get("audit_suppress_skips", _AUDIT_SUPPRESS_DEFAULT)
    except Exception:
        pass
    return reason in set(supp or ())


def _emit_audit(row: dict) -> None:
    """Append one emit decision (arm or skip) — ground truth for diagnostics.

    Routine skips are dropped (see _AUDIT_SUPPRESS_DEFAULT) so the file stays readable.
    """
    try:
        if _audit_suppressed(row):
            return
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

    # Re-adopt any magic the EA reports with live positions that this server has no arm
    # record for (2026-08-05). Runs BEFORE every exit check below so a recovered cycle is
    # managed on the very same poll it is discovered, rather than sitting unmanaged until
    # the next arm. Complements load_persisted_state: that restores what was written to
    # disk, this catches anything that never was.
    if sym and magics:
        try:
            ExecBridge.reconcile_from_poll(account, sym, magics)
        except Exception:
            LOG.exception("[exec] reconcile error")   # never break the poll

    # Combined net target (2026-08-05, user) — account-wide $ check across EVERY open
    # cycle's floating P&L summed together (ported from feat/jul09). Independent of
    # grid_levels.cycle_net_target_usd (per-cycle, unchanged). Checked before the
    # per-magic loop so a hit this poll also blocks any monitor_cycle exits below from
    # racing it — CLOSE_ALL here is authoritative.
    balance, equity = body.get("balance"), body.get("equity")
    if balance is not None and equity is not None:
        combined_target_usd = float((settings_cfg or {}).get("combined_net_target_usd", 0.0) or 0.0)
        if combined_target_usd > 0:
            _is_flat = int(body.get("positions", 0) or 0) == 0 and int(body.get("pendings", 0) or 0) == 0
            combined_result = ExecBridge.check_combined_target(
                account, float(balance), float(equity), combined_target_usd, _is_flat)
            if combined_result["hit"] and sym:
                active_magics = [int(m["magic"]) for m in (magics or [])
                                 if int(m.get("buys", 0)) + int(m.get("sells", 0)) > 0]
                for mg in active_magics:
                    ExecBridge.enqueue(account, "CLOSE_ALL", sym, magic=mg,
                                       comment="FB|combined_target|flatten")
                if active_magics or combined_result["pnl_usd"] >= combined_target_usd:
                    LOG.info(f"[combined_target] account={account} pnl_usd={combined_result['pnl_usd']:.2f} "
                             f">= target={combined_target_usd:.2f} — closing {len(active_magics)} magic(s)")

    if sym and isinstance(magics, list) and magics:
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
    # ABSENT-MAGIC REAP (2026-08-06) — retire arms for magics the EA no longer reports.
    # The retire path lives inside monitor_cycle, but the EA sends "one object per
    # (strategy×TF) magic that HAS any position/pending", so a cycle that goes flat simply
    # stops appearing and monitor_cycle is never called for it again → active=True forever.
    # That blocks the touch-arm path (`if _cyc.get("active"): return`) on that magic
    # permanently. Observed live 2026-08-06: broker fully flat, yet all 7 magics still
    # active — every (trigger, TF) pair blocked. Arm persistence (added earlier the same
    # day) made it survive restarts too, where previously a restart cleared it by accident.
    # Ported from feat/jul09-restored-skew, which has carried this reaper all along.
    if sym and magics is not None:
        try:
            _now_r = time.time()
            _gcfg_r = (settings_cfg or {}).get("grid_levels") or {}
            _reap_grace = float(_gcfg_r.get("reap_grace_s", 30.0) or 30.0)
            _reap_confirm_n = int(_gcfg_r.get("reap_confirm_n", 3) or 3)
            _reap_gap = float(_gcfg_r.get("reap_strike_min_gap_s", 2.0) or 2.0)
            # Whole-account-flat is unambiguous (EA sees zero everything) → reap without
            # the placement grace and without waiting for strikes.
            _flat_all = (int(body.get("positions", 0) or 0) == 0
                         and int(body.get("pendings", 0) or 0) == 0
                         and not magics)
            _reported = {int(m.get("magic", 0)) for m in (magics or [])}
            for _mg in ExecBridge.active_magics(account, sym):
                _cyc = ExecBridge.get_last_arm(account, sym, magic=_mg) or {}
                if _mg in _reported:
                    if _cyc.get("absent_strikes"):   # transient gap resolved — clear strikes
                        ExecBridge.set_last_arm(account, sym, magic=_mg,
                                                **{**{k: v for k, v in _cyc.items() if k != "magic"},
                                                   "absent_strikes": 0})
                    continue
                # Placement-window guard: NEVER reap a cycle armed less than _reap_grace ago,
                # regardless of _flat_all. Originally `if not _flat_all and ...` (ported from
                # jul09) — backwards, because right after an arm the PLACE_PENDING commands
                # are still queued, so the EA reports the account fully flat and _flat_all is
                # TRUE exactly when the guard matters. On jul09 that reaped every cycle ~1s
                # after arming (14:23:08 armed → 14:23:09 reaped), leaving the orders live
                # but the arm inactive, so monitor_cycle never managed them. Grace depends on
                # ARM AGE only; _flat_all may still skip the strike counter below.
                if (_now_r - float(_cyc.get("ts", 0.0) or 0.0)) < _reap_grace:
                    continue
                # Strike counter: a single missing poll can be a payload/enumeration hiccup.
                # Require the absence to persist before retiring a possibly-live cycle.
                if not _flat_all:
                    _strikes = int(_cyc.get("absent_strikes") or 0)
                    if _now_r - float(_cyc.get("absent_last_strike_ts") or 0.0) >= _reap_gap:
                        _strikes += 1
                        ExecBridge.set_last_arm(account, sym, magic=_mg,
                                                **{**{k: v for k, v in _cyc.items() if k != "magic"},
                                                   "absent_strikes": _strikes,
                                                   "absent_last_strike_ts": _now_r})
                    if _strikes < _reap_confirm_n:
                        continue
                ExecBridge.set_last_arm(account, sym, magic=_mg,
                                        **{**{k: v for k, v in _cyc.items() if k != "magic"},
                                           "active": False, "flatten_ts": 0.0,
                                           "absent_strikes": 0})
                ExecBridge.clear_emit(account, sym, magic=_mg)
                ExecBridge.set_open(account, sym, 0, 0, magic=_mg)
                LOG.info(f"[exec] reaped absent magic {_mg} (flat in MT5) for {account}/{sym}")
        except Exception:
            LOG.exception("[exec] absent-magic reap error")   # never break the poll

    # Intrabar touch-arm (2026-08-05, user) — hvn_inside_touch ONLY. hvn_edge and
    # lvn_displacement stay bar-close via /exec/emit_grid. This is the only ~1s hook on
    # this branch, so it is what drops hvn arming latency from a 1m bar close to ~1s.
    _gl_cfg = (settings_cfg or {}).get("grid_levels", {}) or {}
    if sym and bid and ask and bool(_gl_cfg.get("touch_arm_enabled", False)):
        _mid = (float(bid) + float(ask)) / 2.0
        for _tf in (_gl_cfg.get("touch_arm_tfs") or ["5m", "15m"]):
            try:
                _touch_arm_tf(account, sym, str(_tf), settings_cfg or {}, venue_mid=_mid)
            except Exception:
                LOG.exception("[exec] touch_arm error")   # never break the poll

    commands = ExecBridge.poll(account)
    if commands:
        LOG.info(f"[exec] poll account={account} → {len(commands)} command(s)")
    return jsonify({"ok": True, "account": account, "commands": commands})


# ── intrabar touch-arm ───────────────────────────────────────────────────────
# venue↔analysis basis. live quotes are VENUE frame (Vantage XAUUSD.pc); HVN zones are
# ANALYSIS frame (Binance XAUTUSDT). jul09 derives the shift from venue CANDLE CLOSES via
# EA CopyRates, but this branch's server never receives venue bars, so instead the basis is
# sampled once per NEW analysis 1m bar (both sides settled at that instant) and smoothed.
# Sampling at a bar boundary rather than every tick is what keeps the shift a slow-moving
# BASIS instead of absorbing intrabar trend drift — the failure that produced the ~3pt
# double-shift in project_fulcrum_edge_frame_fix. EMA alpha is deliberately low.
_BASIS: dict[tuple, dict] = {}   # (account, broker_symbol) → {"ema": float, "bar_ts": int}
_BASIS_ALPHA = 0.25


def _venue_basis(account: str, broker_symbol: str, analysis_symbol: str,
                 venue_mid: float) -> float:
    """Additive (venue − analysis) basis, re-sampled on each new closed analysis 1m bar."""
    from pipeline.state_store import store
    bars = [b for b in store().recent(analysis_symbol, "1m", 6)
            if b.close_ts and b.close_ts < 9_000_000_000]   # drop sentinel/forming
    if not bars:
        return 0.0
    last = bars[-1]
    key = (str(account), broker_symbol)
    st = _BASIS.get(key)
    if st is None:
        st = {"ema": venue_mid - float(last.ohlc.c), "bar_ts": int(last.close_ts)}
        _BASIS[key] = st
        return st["ema"]
    if int(last.close_ts) != int(st["bar_ts"]):
        sample = venue_mid - float(last.ohlc.c)
        st["ema"] = (1.0 - _BASIS_ALPHA) * st["ema"] + _BASIS_ALPHA * sample
        st["bar_ts"] = int(last.close_ts)
    return float(st["ema"])


def _touch_arm_tf(account: str, broker_symbol: str, tf: str, settings: dict,
                  venue_mid: float) -> None:
    """Resolve the HVN edge live price is tapping, run the tick-reversal confirm, and on
    confirm arm the same straddle the bar-close emit path would — using the live edge as
    fulcrum. No-ops unless a tap is live, the confirm fires, and the magic is free."""
    from execution.grid_planner import plan_grid_levels
    from execution.zone_triggers import touch_arm_trigger
    grid_cfg = settings.get("grid_levels") or {}

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    analysis = {v: k for k, v in symbol_map.items()}.get(broker_symbol, broker_symbol)

    zone_shift = _venue_basis(account, broker_symbol, analysis, venue_mid)
    live_analysis = venue_mid - zone_shift

    trig = touch_arm_trigger(analysis, tf, venue_mid, cfg=grid_cfg, zone_shift=zone_shift)
    if trig is None:
        ExecBridge.clear_touch_state(account, broker_symbol, tf)   # tap abandoned
        return

    edge = float(trig.fulcrum_price)
    side = str(trig.context.get("edge", ""))
    confirm_ticks = float(grid_cfg.get("touch_arm_confirm_ticks", 0.2) or 0.0)
    if not ExecBridge.touch_arm_check(account, broker_symbol, tf, live_analysis,
                                      edge, side, confirm_ticks):
        return   # tap recorded, awaiting the reversal back inside

    leg_magic = magic_for("hvn_inside_touch", tf)
    open_state = ExecBridge.get_open(account, broker_symbol, magic=leg_magic) or {}
    if (int(open_state.get("positions", 0) or 0) > 0
            or int(open_state.get("pendings", 0) or 0) > 0):
        return   # magic occupied — one concurrent cycle per (setup × TF)
    _cyc = ExecBridge.get_last_arm(account, broker_symbol, magic=leg_magic) or {}
    if _cyc.get("active"):
        return

    ExecBridge.clear_emit(account, broker_symbol, magic=leg_magic)
    dedup_tol = venue_mid * float(grid_cfg.get("emit_dedup_pct", 0.0007) or 0.0)
    if not ExecBridge.should_emit(account, broker_symbol, round(edge + zone_shift, 4),
                                  dedup_tol, magic=leg_magic):
        return

    quote = ExecBridge.get_quote(account, broker_symbol) or {}
    min_step_venue = float(quote.get("stops_dist", 0.0) or 0.0) * 1.5
    plan = plan_grid_levels(analysis, tf, live_analysis,
                            trigger_hint="hvn_inside_touch", settings=settings,
                            venue_price=venue_mid, min_step_venue=min_step_venue,
                            force_trigger=trig)
    if plan.verdict != "arm" or plan.trigger_kind != "hvn_inside_touch":
        _emit_audit({"account": account, "symbol": analysis, "tf": tf, "verdict": "skip",
                     "skip_reason": f"touch_arm:{plan.skip_reason or 'no_plan'}",
                     "touch_armed": True, "hint": "hvn_inside_touch"})
        return

    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan, close_first=True,
                                        clear_kind="cancel", magic=leg_magic, tf=tf)
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, magic=leg_magic,
                            fulcrum=plan.fulcrum, edge=side, trigger_kind=plan.trigger_kind,
                            venue_mid=venue_mid, n_per_side=plan.n_per_side, step=plan.step,
                            ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            bias_peak=0.0, bias_booked=False,
                            active=True, armed_tf=tf,
                            tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank)
    ExecBridge.mark_emit(account, broker_symbol, plan.fulcrum, magic=leg_magic)
    _emit_audit({"account": account, "symbol": analysis, "broker_symbol": broker_symbol,
                 "tf": tf, "verdict": "arm", "trigger_kind": plan.trigger_kind, "edge": side,
                 "fulcrum": plan.fulcrum, "venue_mid": venue_mid, "touch_armed": True,
                 "zone_shift": round(zone_shift, 4),
                 "n_per_side": plan.n_per_side, "step": plan.step,
                 "buy_tp": plan.buy_tp, "sell_tp": plan.sell_tp, "hint": "hvn_inside_touch"})
    LOG.info(f"[exec] TOUCH-ARM {account} {broker_symbol} {tf} → armed "
             f"[edge={side} fulcrum={plan.fulcrum}], {len(cmds)} command(s)")


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

    # Combined net target gate (2026-08-05, user) — no new arms once the account-wide
    # $ target has fired for this account. Clears automatically once flat (see
    # check_combined_target in execution/exec_bridge.py).
    if ExecBridge.combined_target_hit(account):
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": "combined_target_hit",
                        "symbol": req_symbol})

    # broker↔analysis symbol resolution (same map as /grid_levels)
    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    symbol = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(symbol, req_symbol)

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
    # Touch-arm owns hvn_inside_touch (2026-08-05, user) — suppress it on the BAR-CLOSE
    # path so the two never contend for the same magic. Done server-side rather than by
    # stripping the kind from the EA's hint string: the EA still sends
    # "hvn_inside_touch,hvn_edge", and hvn_edge must keep arming from that same call.
    # hvn_edge / lvn_displacement / vp_levels are unaffected and stay bar-close.
    _touch_owns = (bool((settings.get("grid_levels") or {}).get("touch_arm_enabled", False))
                   and plan.trigger_kind == "hvn_inside_touch"
                   and tf in ((settings.get("grid_levels") or {}).get("touch_arm_tfs")
                              or ["5m", "15m"]))
    if _touch_owns:
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": "owned_by_touch_arm", "hint": trigger_hint})
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": "owned_by_touch_arm", "symbol": symbol})
    if plan.verdict != "arm":
        # episode ended → next arm on this magic is a fresh touch
        ExecBridge.clear_emit(account, symbol, magic=leg_magic)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": plan.skip_reason, "hint": trigger_hint})
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": plan.skip_reason,
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Strict: plan_grid_levels falls back to ALL triggers when the hinted one is
    # absent — but emit must fire ONLY a requested-group strategy, never a stray
    # hvn_edge/va/imbalance grid. Membership (not equality) so the group works.
    hint_set = _hint_set(trigger_hint)
    if hint_set and plan.trigger_kind not in hint_set:
        ExecBridge.clear_emit(account, symbol, magic=leg_magic)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": f"trigger_mismatch:{plan.trigger_kind}", "hint": trigger_hint})
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
                     "skip_reason": "dedup:same_fulcrum", "fulcrum": plan.fulcrum, "hint": trigger_hint})
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
                 "squeeze_ok": plan.squeeze_ok, "squeeze_rank": plan.squeeze_rank,
                 "hint": trigger_hint})
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
    zone_tf = str(body.get("tf") or "")

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

    # Prior days' cached HVN/LVN (up to 4 more trading days, oldest weekend-excluded
    # per build_and_save) — tagged "hvn_prev"/"lvn_prev", each carrying its own "day"
    # (period_key) so the EA can label which calendar day it came from. DrawZone's
    # rectangle still isn't time-anchored to its own session (see the "profiles"
    # histograms below for the actual per-day breakdown) — this is a text label only.
    hist = vp_cache.get_history(symbol, "daily", n=5)  # oldest → newest
    prior_days = hist[:-1] if len(hist) > 1 else []

    # Which prior day (if any) the LIVE hvn_inside_touch vacuum-fallback would borrow
    # from right now — mirrors execution.zone_triggers._prior_day_hvn/_outside_daily_va
    # exactly (newest-first, first day whose value area brackets current price) so the
    # chart can highlight that ONE day distinctly instead of leaving all 4 equal.
    from pipeline.state_store import store as _store3
    _latest_bar = _store3().latest(symbol, "1m")
    _cur_price = float(_latest_bar.ohlc.c) if _latest_bar else 0.0
    active_day_key = None
    if _cur_price > 0:
        from execution.zone_triggers import _outside_daily_va
        if _outside_daily_va(symbol, _cur_price):
            for e in reversed(prior_days):
                val, vah = e.get("val"), e.get("vah")
                if val is None or vah is None:
                    continue
                if float(val) <= _cur_price <= float(vah):
                    active_day_key = e.get("period_key")
                    break

    for e in prior_days:
        day_key = e.get("period_key", "")
        is_active = day_key == active_day_key and day_key is not None
        hvn_kind = "hvn_prev_active" if is_active else "hvn_prev"
        lvn_kind = "lvn_prev_active" if is_active else "lvn_prev"
        for z in (e.get("hvn_zones") or []):
            zones.append({"kind": hvn_kind, "lo": round(float(z["low"]), 5),
                          "hi": round(float(z["high"]), 5), "day": day_key})
        for z in (e.get("lvn_zones") or []):
            zones.append({"kind": lvn_kind, "lo": round(float(z["low"]), 5),
                          "hi": round(float(z["high"]), 5), "day": day_key})

    # Rolling (price-tracking) HVN/LVN — visual-only, tagged "hvn_roll"/"lvn_roll" so
    # the EA can draw them distinctly from the cached-daily zones above. This is NOT
    # what the grid trigger uses for arming necessarily (that's session-gated via
    # _SESSION_HVN_SRC) — it's always computed here so rolling vs cached can be
    # eyeballed on chart regardless of session, same idea as the existing fine/tick
    # A/B overlay pair. Withheld (empty) until the TF has enough bars for its window.
    try:
        from pipeline.state_store import store as _store
        from execution.zone_triggers import _rolling_hvn, _rolling_lvn, _VP_WIN
        roll_tf = zone_tf if zone_tf in _VP_WIN else "15m"
        roll_bars = _store().recent(symbol, roll_tf, _VP_WIN.get(roll_tf, 96))
        for lo, hi in _rolling_hvn(symbol, roll_tf, roll_bars):
            zones.append({"kind": "hvn_roll", "lo": round(lo, 5), "hi": round(hi, 5)})
        for lo, hi in _rolling_lvn(symbol, roll_tf, roll_bars):
            zones.append({"kind": "lvn_roll", "lo": round(lo, 5), "hi": round(hi, 5)})
    except Exception:
        pass

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

    # Per-day session-anchored histograms (last 5 trading days, oldest first) — the EA
    # draws one histogram per entry via "profiles", each anchored at its own start_ts.
    # Supersedes the flat "profile" above for anyone drawing multi-day; "profile"/"vp_bin"
    # stay for older EA builds using the single-histogram fallback.
    profiles = vp_cache.period_profiles_session(symbol, days=5)

    quote = ExecBridge.get_quote(account, broker_symbol) or {}
    # dashboard shows the cycle for the EA's drawn TF (body.tf). Cycles are now per-TF,
    # so without a tf we'd find nothing — default to the zone TF the EA reports.
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

    # VENUE SHIFT AT THE BOUNDARY (2026-08-06). vp_cache.get()/get_history() now return
    # ANALYSIS frame — every detector reads through them and plan_grid_levels owns the
    # single venue rebase, so shifting inside get() silently put venue-frame zones into
    # analysis-frame comparisons and stopped HVN touches arming. The chart still wants
    # venue frame, so the shift is applied HERE, where the payload leaves for the EA.
    # `profile`/`profiles` are NOT touched — period_profile* resolve the offset themselves.
    _zoff = _venue_offset_for(symbol)
    if _zoff:
        zones = [{**z, "lo": round(float(z["lo"]) + _zoff, 5),
                  "hi": round(float(z["hi"]) + _zoff, 5)} for z in zones]
        levels = [{**lv, "price": round(float(lv["price"]) + _zoff, 5)}
                  if lv.get("price") is not None else lv for lv in (levels or [])]

    return jsonify({"ok": True, "zones": zones, "levels": levels, "ict": ict_out,
                    "profile": profile, "vp_bin": vp_bin, "profiles": profiles,
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
