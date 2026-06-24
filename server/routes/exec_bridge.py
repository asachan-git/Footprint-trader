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


def _refresh_cycle_tps(account: str, broker_symbol: str, analysis_symbol: str,
                       tf: str, magic: int, settings: dict) -> None:
    """Track the moving HVN for ONE active cycle (called each poll). Recompute the
    fulcrum edge + structural TPs from the latest daily HVN zones; if they shifted,
    MODIFY_PENDING (entry prices + TP) and MODIFY_POSITION (filled-leg TP, keep SL) so
    BOTH pending and open orders chase the live HVN — never stranded on arm-time values.

    Works for pending-only cycles (no fill yet) and filled cycles alike. No-op unless the
    cycle is active and the recomputed edge/TP actually moved beyond the noise floor."""
    from execution.zone_triggers import compute_hvn_tps
    from pipeline.features.vp_cache import get as vp_get
    from pipeline.state_store import store

    arm = ExecBridge.get_last_arm(account, broker_symbol, magic=magic)
    if not arm or not arm.get("active"):
        return
    quote = ExecBridge.get_quote(account, broker_symbol) or {}
    venue_mid = float(quote.get("mid") or 0.0)
    if venue_mid <= 0:
        return
    latest = store().latest(analysis_symbol, tf)
    if latest is None or not latest.ohlc.c:
        return
    ratio = venue_mid / float(latest.ohlc.c) if latest.ohlc.c else 1.0
    old_fulcrum_venue = float(arm.get("fulcrum") or 0.0)
    edge_raw = old_fulcrum_venue / ratio if ratio else old_fulcrum_venue

    open_state = ExecBridge.get_open(account, broker_symbol, magic=magic) or {}
    _open_buys = int((open_state.get("buys") or 0))
    _open_sells = int((open_state.get("sells") or 0))

    # FROZEN cycle: once a leg filled, the VP/HVN structure was snapshotted (monitor_cycle).
    # We manage against THAT frozen structure — fulcrum is LOCKED (no shift/re-anchor), no
    # fade-flatten (morphing live VP is irrelevant; the entry's structure is fixed). TP is
    # recomputed from the frozen zones (stable, since they don't change). Exits come from
    # BE / bias-trail / net-target / price-hits-TP, never structure morph.
    _frozen = arm.get("vp_frozen") and arm.get("frozen_zones")
    if _frozen:
        _raw_zones = [(float(lo), float(hi)) for lo, hi in arm.get("frozen_zones") or []]
        best_edge = edge_raw          # fulcrum locked — no shift
        # fall straight through to the TP recompute below (no drift/fade branch)
    else:
        # Live (pending-only, pre-fill) cycle: zone source MUST match what the trigger armed
        # against — session HVNs (rolling today + cached prev-D), NOT daily-only (which omits
        # the forming hvn_today node and false-faded). Re-anchor pendings cheaply while flat.
        try:
            from execution.zone_triggers import _session_hvn_zones
            _sbars = store().recent(analysis_symbol, tf, 120)
            _raw_zones, _ = _session_hvn_zones(analysis_symbol, tf, _sbars)
        except Exception:
            _raw_zones = []
        if not _raw_zones:
            _dvp = vp_get(analysis_symbol, "daily") or {}
            _raw_zones = [(float(z["low"]), float(z["high"])) for z in (_dvp.get("hvn_zones") or [])]
        if not _raw_zones:
            return

    # ── Anchor-HVN structural-integrity check (LIVE/pre-fill cycles ONLY) ────────────
    # Skipped entirely for a FROZEN cycle: once a leg fills, the fulcrum is locked to the
    # snapshot structure — no drift, no re-anchor, no fade-flatten. Pending-only cycles
    # (not yet frozen) still classify the anchor node each poll:
    #   DRIFT  — nearest edge moved ≤ 2×step          → shift pendings to track it
    #   RE-ANCHOR — edge moved > 2×step BUT a node still brackets the fulcrum (merge/split)
    #   FADED  — no node brackets AND nearest edge > 2×step → premise gone → flatten+retire
    if not _frozen:
        _step_venue = float(arm.get("step") or 0.0)
        _step_raw = _step_venue / ratio if ratio else _step_venue
        _drift_cap = 2.0 * _step_raw if _step_raw > 0 else (5.0)   # raw frame

        best_dist, best_edge = float("inf"), edge_raw
        for lo, hi in _raw_zones:
            for cand in (lo, hi):
                d = abs(cand - edge_raw)
                if d < best_dist:
                    best_dist, best_edge = d, cand
        _bracketed = any(lo <= edge_raw <= hi for lo, hi in _raw_zones)
        raw_delta = best_edge - edge_raw
        venue_delta = round(raw_delta * ratio, 4)

        # Placement-window grace: don't fade-flatten a cycle armed in the last 30s — its
        # legs may not be placed/reported yet, and a mid-rebuild HVN read could be noise.
        import time as _t
        _arm_age = _t.time() - float(arm.get("ts", 0.0) or 0.0)
        if (not _bracketed) and best_dist > _drift_cap and _arm_age > 30.0:
            # FADED — anchor HVN dissolved before any fill. Premise invalidated → flatten.
            ExecBridge.enqueue(account, "CLOSE_ALL", broker_symbol, magic=magic,
                               comment="FB|hvn_faded")
            _cyc = ExecBridge.get_last_arm(account, broker_symbol, magic=magic) or {}
            _cyc.pop("magic", None)
            ExecBridge.set_last_arm(account, broker_symbol, magic=magic,
                                    **{**_cyc, "active": False, "flatten_ts": __import__("time").time()})
            ExecBridge.clear_emit(account, broker_symbol, magic=magic)
            _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                         "verdict": "hvn_faded_flatten", "magic": magic, "poll": True,
                         "exit_reason": "hvn_faded",
                         "old_fulcrum": old_fulcrum_venue, "nearest_edge_dist": round(best_dist, 4)})
            return   # cycle retired — nothing more to refresh

        # DRIFT or RE-ANCHOR: shift fulcrum + pendings to best_edge (still on real structure).
        if abs(venue_delta) > 0.1:
            _kind = "fulcrum_shift" if best_dist <= _drift_cap else "fulcrum_reanchor"
            new_fulcrum_venue = round(old_fulcrum_venue + venue_delta, 4)
            arm = {**arm, "fulcrum": new_fulcrum_venue}
            ExecBridge.set_last_arm(account, broker_symbol, **arm)
            ExecBridge.enqueue_modify_pending(account, broker_symbol, magic, price_delta=venue_delta)
            _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                         "verdict": _kind, "magic": magic, "poll": True,
                         "old_fulcrum": old_fulcrum_venue, "new_fulcrum": new_fulcrum_venue,
                         "delta": venue_delta, "bracketed": _bracketed})

    # recompute TPs from the (possibly shifted) edge, with the min-distance floor
    # (analysis frame: min_tp_dist is venue-$, un-rebase by /ratio)
    _min_tp = float(settings.get("grid_levels", {}).get("min_tp_dist", 0.0) or 0.0)
    _min_tp_raw = _min_tp / ratio if ratio else _min_tp
    # Reconstruct the outermost ladder legs (analysis frame) from the arm's geometry so the
    # refreshed TP clears the WHOLE ladder — matching the arm-time computation. Without this
    # the TP is measured from the edge only and can regress INSIDE the ladder (un-profitable).
    # per-side leg counts (skew-aware): buy_n legs above the edge, sell_n below.
    _buy_n  = int(arm.get("buy_n")  or arm.get("n_per_side") or 0)
    _sell_n = int(arm.get("sell_n") or arm.get("n_per_side") or 0)
    _stp_venue = float(arm.get("step") or 0.0)
    _stp_raw   = _stp_venue / ratio if ratio else _stp_venue
    _top_leg = best_edge + _buy_n  * _stp_raw if (_buy_n  > 0 and _stp_raw > 0) else best_edge
    _bot_leg = best_edge - _sell_n * _stp_raw if (_sell_n > 0 and _stp_raw > 0) else best_edge
    # Unified TP rule (matches arm-time): next HVN far edge + VP refinements (Case 1: VP
    # within 1×step of HVN edge → VP; Case 2: HVN < 2×step from leg → next VP/HVN beyond).
    # Refreshed each poll; HVN rebuilds every 15m so the target re-targets on that cadence.
    # hvn_inside_touch: target the NEXT node's far edge — exclude the node the fulcrum
    # sits in (arm-time bounds stored venue-frame → un-rebase to analysis/raw via /ratio).
    _skip_node = None
    if arm.get("trigger_kind") == "hvn_inside_touch" and ratio:
        _nlo_v = float(arm.get("node_low") or 0.0)
        _nhi_v = float(arm.get("node_high") or 0.0)
        if _nlo_v > 0 and _nhi_v > _nlo_v:
            _skip_node = (_nlo_v / ratio, _nhi_v / ratio)
    from execution.zone_triggers import hvn_or_vp_tp as _hvn_or_vp_tp
    raw_tp_up, raw_tp_down = _hvn_or_vp_tp(analysis_symbol, _raw_zones, _top_leg, _bot_leg,
                                           _stp_raw, min_tp_dist=_min_tp_raw, skip_node=_skip_node)
    # Cascade fallback ONLY when nothing structural sits beyond the leg (LVN→fib).
    if raw_tp_up == 0.0 or raw_tp_down == 0.0:
        _c_up, _c_dn = compute_hvn_tps(analysis_symbol, best_edge, _raw_zones, skip_node=_skip_node,
                                       min_dist=_min_tp_raw, top_leg=_top_leg, bot_leg=_bot_leg)
        if raw_tp_up   == 0.0: raw_tp_up   = _c_up
        if raw_tp_down == 0.0: raw_tp_down = _c_dn
    # Guard: TP must lie strictly beyond the outermost leg or the grid can't profit on that
    # side — drop it to 0 (leave the existing TP untouched) rather than set an inside-ladder TP.
    if not (raw_tp_up   and raw_tp_up   > _top_leg):       raw_tp_up   = 0.0
    if not (raw_tp_down and 0 < raw_tp_down < _bot_leg):   raw_tp_down = 0.0
    new_tp_up = round(raw_tp_up * ratio, 4) if raw_tp_up else 0.0
    new_tp_down = round(raw_tp_down * ratio, 4) if raw_tp_down else 0.0
    old_up = float(arm.get("tp_up") or 0.0)
    old_down = float(arm.get("tp_down") or 0.0)
    if (new_tp_up and abs(new_tp_up - old_up) > 0.05) or (new_tp_down and abs(new_tp_down - old_down) > 0.05):
        ExecBridge.set_last_arm(account, broker_symbol,
                                **{**arm, "tp_up": new_tp_up, "tp_down": new_tp_down})
        if new_tp_up:
            ExecBridge.enqueue_modify_pending(account, broker_symbol, magic,
                                              price_delta=0.0, new_tp=new_tp_up, side="buy")
            if _open_buys:
                ExecBridge.enqueue_modify_position(account, broker_symbol, magic,
                                                   new_tp=new_tp_up, side="buy",
                                                   comment="FB|tp_refresh|buy")
        if new_tp_down:
            ExecBridge.enqueue_modify_pending(account, broker_symbol, magic,
                                              price_delta=0.0, new_tp=new_tp_down, side="sell")
            if _open_sells:
                ExecBridge.enqueue_modify_position(account, broker_symbol, magic,
                                                   new_tp=new_tp_down, side="sell",
                                                   comment="FB|tp_refresh|sell")
        _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                     "verdict": "tp_refresh", "magic": magic, "poll": True,
                     "tp_up_old": old_up, "tp_down_old": old_down,
                     "tp_up": new_tp_up, "tp_down": new_tp_down})


def _cancel_orphan_on_hvn_gone(account: str, broker_symbol: str, analysis_symbol: str,
                               tf: str, magic: int, open_buys: int, open_sells: int,
                               pendings: int, settings: dict) -> bool:
    """Cancel a never-filled resting pending leg whose anchoring (fulcrum) HVN has
    DISAPPEARED from the live VP — but ONLY once the rest of the cycle is wound down.

    Conditions (ALL must hold), per the spec "trigger to their HVNs and if that HVN
    disappears we cancel one side if it was pending without execution and other trades
    were closed":
      1. cycle is active and HAD filled at some point (max_pos_seen > 0) — so a freshly
         armed straddle that just hasn't filled yet is NOT cancelled prematurely;
      2. no open positions remain (open_buys + open_sells == 0) — the other legs closed;
      3. a pending leg still rests (pendings > 0) — the orphan dangler;
      4. the fulcrum HVN is gone — the arm's fulcrum no longer sits inside any current
         HVN zone (within a tolerance band) in the live daily VP.

    On all-true: enqueue CANCEL_PENDINGS scoped to this magic (positions are already 0,
    so it can never flatten a live trade) and retire the cycle. Returns True if it acted.
    Gated by grid_levels.cancel_orphan_on_hvn_gone (default off — opt-in)."""
    gcfg = (settings.get("grid_levels") or {})
    if not bool(gcfg.get("cancel_orphan_on_hvn_gone", False)):
        return False
    if pendings <= 0 or (open_buys + open_sells) > 0:
        return False

    arm = ExecBridge.get_last_arm(account, broker_symbol, magic=magic)
    if not arm or not arm.get("active"):
        return False
    if int(arm.get("max_pos_seen") or 0) <= 0:
        return False   # never filled → not an orphan dangler, leave the fresh straddle

    from pipeline.features.vp_cache import get as vp_get
    from pipeline.state_store import store

    latest = store().latest(analysis_symbol, tf)
    if latest is None or not latest.ohlc.c:
        return False
    quote = ExecBridge.get_quote(account, broker_symbol) or {}
    venue_mid = float(quote.get("mid") or 0.0)
    if venue_mid <= 0:
        return False
    ratio = venue_mid / float(latest.ohlc.c) if latest.ohlc.c else 1.0

    _dvp = vp_get(analysis_symbol, "daily") or {}
    zones = [(float(z["low"]), float(z["high"])) for z in (_dvp.get("hvn_zones") or [])]
    if not zones:
        return False   # no VP yet → don't fabricate "gone"; wait for a real profile

    # arm["fulcrum"] is venue-frame (matches _refresh_cycle_tps); un-rebase to analysis.
    fulcrum_raw = float(arm.get("fulcrum") or 0.0) / ratio if ratio else 0.0
    if fulcrum_raw <= 0:
        return False
    # tolerance band (analysis price) — config is venue-$, un-rebase by /ratio.
    tol = float(gcfg.get("hvn_gone_tol_usd", 0.0) or 0.0) / ratio if ratio else 0.0
    in_hvn = any(lo - tol <= fulcrum_raw <= hi + tol for lo, hi in zones)
    if in_hvn:
        return False   # fulcrum HVN still present → keep the pending armed

    ExecBridge.enqueue(account, "CANCEL_PENDINGS", broker_symbol, magic=magic,
                       comment="FB|orphan|hvn_gone")
    ExecBridge.set_last_arm(account, broker_symbol, **{**arm, "active": False})
    _emit_audit({"account": account, "symbol": analysis_symbol, "broker_symbol": broker_symbol,
                 "tf": tf, "verdict": "orphan_cancel_hvn_gone", "magic": magic, "poll": True,
                 "fulcrum_raw": round(fulcrum_raw, 5), "pendings": pendings,
                 "n_zones": len(zones)})
    LOG.info(f"[exec] ORPHAN-CANCEL {account} {broker_symbol} {tf} magic={magic} — "
             f"fulcrum HVN gone, {pendings} pending(s) cancelled (no open positions)")
    return True


def _touch_arm_tf(account: str, broker_symbol: str, tf: str, settings: dict) -> None:
    """Intrabar touch-arm for ONE touch-enabled TF, called each poll. Resolves the
    HVN edge live price is tapping, runs the tick-reversal confirm, and on confirm
    arms the same straddle the close-driven emit would — using the live edge as the
    fulcrum (no candle-close wait). Server stays the brain; the EA only reports price.

    No-ops unless: touch_arm_enabled, tf in touch_arm_tfs, a venue quote is cached,
    the symbol is flat on this magic, and the fulcrum is a NEW episode (dedup)."""
    from execution.grid_planner import plan_grid_levels
    from execution.zone_triggers import touch_arm_trigger
    grid_cfg = settings.get("grid_levels") or {}
    if not bool(grid_cfg.get("touch_arm_enabled", False)):
        return
    if tf not in (grid_cfg.get("touch_arm_tfs") or []):
        return

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    analysis = broker_to_analysis.get(broker_symbol, broker_symbol)

    quote = ExecBridge.get_quote(account, broker_symbol)
    if not quote or not quote.get("mid"):
        return
    venue_mid = float(quote["mid"])

    # un-rebase venue mid → analysis frame so the edge lookup matches stored zones
    from pipeline.state_store import store
    latest = store().latest(analysis, tf)
    if latest is None or not latest.ohlc.c:
        return
    ratio = venue_mid / float(latest.ohlc.c) if latest.ohlc.c else 1.0
    live_analysis = venue_mid / ratio if ratio else venue_mid

    trig = touch_arm_trigger(analysis, tf, live_analysis)
    if trig is None:
        ExecBridge.clear_touch_state(account, broker_symbol, tf)   # left the buffer → reset
        return

    edge = float(trig.fulcrum_price)
    side = str(trig.context.get("edge", ""))
    # confirm distance: tick-reversal back inside, in analysis-frame price units
    confirm_ticks = float(grid_cfg.get("touch_arm_confirm_ticks", 0.2) or 0.2)
    if not ExecBridge.touch_arm_check(account, broker_symbol, tf, live_analysis,
                                      edge, side, confirm_ticks):
        return   # tap recorded; awaiting reversal-back-inside (or it's a breakout)

    # confirmed mini-rejection → arm. Gate on flat + dedup (same as emit).
    leg_magic = magic_for("hvn_inside_touch", tf)
    open_state = ExecBridge.get_open(account, broker_symbol, magic=leg_magic) or {}
    if int(open_state.get("positions", 0) or 0) > 0 or int(open_state.get("pendings", 0) or 0) > 0:
        return   # cycle already live on this magic
    # One concurrent cycle per TF: an ACTIVE arm occupies this magic even when MT5 momentarily
    # reports flat (legs placed but not yet reported back, or pendings mid-reanchor). Without
    # this, a subsequent touch could stack a second straddle on the same node before the first
    # cycle's legs surface. Re-arm is freed only when the cycle retires (active→False on
    # completion / reap / fade). Subsequent touches → only ONE grid per TF.
    if (ExecBridge.get_last_arm(account, broker_symbol, magic=leg_magic) or {}).get("active"):
        return
    dedup_pct = float(grid_cfg.get("emit_dedup_pct", 0.0007) or 0.0)
    dedup_tol = venue_mid * dedup_pct
    fulcrum_venue = round(edge * ratio, 4)
    if not ExecBridge.should_emit(account, broker_symbol, fulcrum_venue, dedup_tol, magic=leg_magic):
        return

    min_step_venue = float(quote.get("stops_dist", 0.0) or 0.0) * 1.5
    plan = plan_grid_levels(analysis, tf, live_analysis,
                            trigger_hint="hvn_inside_touch", settings=settings,
                            venue_price=venue_mid, min_step_venue=min_step_venue,
                            force_trigger=trig)
    if plan.verdict != "arm" or plan.trigger_kind != "hvn_inside_touch":
        _emit_audit({"account": account, "symbol": analysis, "tf": tf, "verdict": "skip",
                     "skip_reason": f"touch_arm:{plan.skip_reason or 'no_plan'}",
                     "touch_armed": True})
        return

    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan,
                                        close_first=True, clear_kind="cancel",
                                        magic=leg_magic, leg_tp=True)
    _ratio = (plan.venue_anchor / plan.analysis_anchor) if plan.analysis_anchor else 1.0
    node_low = float(plan.trigger_context.get("node_low", 0.0) or 0.0) * _ratio
    node_high = float(plan.trigger_context.get("node_high", 0.0) or 0.0) * _ratio
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=side,
                            trigger_kind=plan.trigger_kind, venue_mid=venue_mid, magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            bias_peak=0.0, bias_booked=False,
                            be_done_buy=False, be_done_sell=False,
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank)
    ExecBridge.mark_emit(account, broker_symbol, plan.fulcrum, magic=leg_magic)
    _emit_audit({"account": account, "symbol": analysis, "broker_symbol": broker_symbol,
                 "tf": tf, "verdict": "arm", "trigger_kind": plan.trigger_kind, "edge": side,
                 "fulcrum": plan.fulcrum, "venue_mid": venue_mid, "touch_armed": True,
                 "n_per_side": plan.n_per_side, "step": plan.step,
                 "buy_tp": plan.buy_tp, "sell_tp": plan.sell_tp})
    LOG.info(f"[exec] TOUCH-ARM {account} {broker_symbol} {tf} → armed "
             f"[edge={side} fulcrum={plan.fulcrum}], {len(cmds)} command(s)")


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

    # Daily floating P&L target — check on every poll that carries balance/equity.
    balance = body.get("balance")
    equity  = body.get("equity")
    if balance is not None and equity is not None:
        target_pct = float((settings_cfg or {}).get("daily_target_pct", 0.0) or 0.0)
        result = ExecBridge.update_account_balance(
            account, float(balance), float(equity), target_pct)
        if result["hit"] and sym:
            # enqueue CLOSE_ALL for every active magic and log once per trigger
            magics_body = body.get("magics") or []
            active_magics = [int(m["magic"]) for m in magics_body
                             if int(m.get("buys", 0)) + int(m.get("sells", 0)) > 0]
            for mg in active_magics:
                ExecBridge.enqueue(account, "CLOSE_ALL", sym, magic=mg,
                                   comment="FB|daily_target|flatten")
            if active_magics or result["pnl_pct"] >= target_pct:
                LOG.info(f"[daily_target] account={account} pnl={result['pnl_pct']:.2f}% "
                         f">= target={target_pct}% — closing {len(active_magics)} magic(s)")
    # Per-magic open-state + cycle monitor. The EA sends a `magics` array — one entry
    # per (strategy×TF) pool it holds — so each TF cycle is tracked and exited in
    # isolation. tf is recovered from the magic. A flatten ships in the same response
    # (saves a ~1s round-trip). Falls back to the legacy aggregate fields for an older
    # EA binary (single pool, no per-magic breakdown).
    magics = body.get("magics")
    # analysis symbol (Bybit frame) for HVN lookups — resolve once for the refresh below
    _symmap = (settings_cfg or {}).get("execution", {}).get("symbol_map") or {}
    _b2a = {v: k for k, v in _symmap.items()}
    analysis_sym = _b2a.get(sym, sym) if sym else sym
    if sym and isinstance(magics, list) and magics:
        # Reconcile: any magic with live positions but no _last_arm entry was
        # orphaned by a Flask restart. Stub it so monitor_cycle tracks it.
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
                ExecBridge.set_open(account, sym, b + s, int(m.get("pendings", 0)),
                                    tf=tf_m, magic=mg, buys=b, sells=s)
                ExecBridge.monitor_cycle(account, sym, settings_cfg, tf=tf_m, magic=mg,
                                         pnl=float(m.get("pnl", 0.0)), buys=b, sells=s,
                                         buy_pnl=float(m.get("buy_pnl", 0.0)),
                                         sell_pnl=float(m.get("sell_pnl", 0.0)))
                # NOTE: HVN-driven TP/entry refresh runs on CANDLE CLOSE only (in the
                # /exec/emit_grid position_open path), NOT here on the 1s poll — by design,
                # so orders re-target structure once per bar, not every tick.
                # Orphan-pending sweep: if this cycle's positions all closed and a never-
                # filled pending still rests on a fulcrum HVN that has since disappeared,
                # cancel that dangling pending (opt-in via grid_levels.cancel_orphan_on_hvn_gone).
                _cancel_orphan_on_hvn_gone(account, sym, analysis_sym, tf_m, mg,
                                           b, s, int(m.get("pendings", 0)), settings_cfg or {})
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

    # Reap absent magics: any cycle the server still holds ACTIVE but the EA did NOT
    # report THIS poll has zero live positions AND pendings in MT5 — BuildMagicsJson emits
    # a magic only when it iterates a live order/position, so absence == flat. Retire it so
    # phantom cycles (manually flattened in the terminal) can't block re-arm or linger in
    # the dashboard. Runs whenever the EA sends a magics array (even empty []); never on the
    # legacy single-pool path (no per-magic truth there). clear_emit frees the fulcrum dedup.
    if sym and isinstance(magics, list):
        try:
            import time as _t
            _now = _t.time()
            _reap_grace = 30.0   # s — let a freshly-armed cycle place + report its legs first
            # Whole-account-flat signal: EA reports zero buys/sells/pendings at the top level
            # AND an empty magics array → MT5 holds nothing for us. Unambiguous; reap WITHOUT
            # the placement grace (no fresh arm can have legs if the EA sees zero everything).
            _flat_all = (int(body.get("buys", 0) or 0) == 0
                         and int(body.get("sells", 0) or 0) == 0
                         and int(body.get("pendings", 0) or 0) == 0
                         and not magics)
            _reported = {int(m.get("magic", 0)) for m in magics}
            for _mg in list(ExecBridge.active_fulcrums(account, sym).keys()):
                if _mg in _reported:
                    continue
                _cyc = ExecBridge.get_last_arm(account, sym, magic=_mg) or {}
                # Placement-window guard: skip a just-armed cycle whose EA legs haven't
                # been reported yet (else we'd retire a fresh arm before it places).
                # Skipped entirely when the EA reports the whole account flat.
                if not _flat_all and (_now - float(_cyc.get("ts", 0.0) or 0.0)) < _reap_grace:
                    continue
                if _cyc:
                    # explicit magic=_mg: persisted arms may lack a `magic` key, which would
                    # otherwise default set_last_arm's key to 0 and leave the real entry active.
                    _cyc.pop("magic", None)
                    ExecBridge.set_last_arm(account, sym, magic=_mg, **{**_cyc, "active": False})
                ExecBridge.clear_emit(account, sym, magic=_mg)
                ExecBridge.set_open(account, sym, 0, 0, magic=_mg)
                LOG.info(f"[exec] reaped absent magic {_mg} (flat in MT5) for {account}/{sym}")
        except Exception:
            LOG.exception("[exec] absent-magic reap error")  # never break the poll

    # Intrabar touch-arm — check each touch-enabled TF against live price (this is the
    # only 1s-cadence hook). Gated off unless touch_arm_enabled; never breaks the poll.
    if sym and not ExecBridge.daily_target_hit(account):
        for _tf in ((settings_cfg or {}).get("grid_levels", {}).get("touch_arm_tfs") or []):
            try:
                _touch_arm_tf(account, sym, _tf, settings_cfg or {})
            except Exception:
                LOG.exception("[exec] touch_arm error")  # never break the poll

    commands = ExecBridge.poll(account)
    if commands:
        LOG.info(f"[exec] poll account={account} → {len(commands)} command(s)")
    # Per-magic fulcrums so the EA's dashboard computes hedged loss per cycle against
    # ITS OWN fulcrum (not one shared value — which conflates parallel cycles).
    fulcrums = ExecBridge.active_fulcrums(account, sym) if sym else {}
    fulcrums_arr = [{"magic": mg, "fulcrum": fx} for mg, fx in fulcrums.items()]
    return jsonify({"ok": True, "account": account, "commands": commands,
                    "fulcrums": fulcrums_arr})


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

    # Daily target gate — no new arms once today's P&L target is hit
    if ExecBridge.daily_target_hit(account):
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": "daily_target_hit"})

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
    _live = int(open_state.get("positions", 0) or 0) > 0 or int(open_state.get("pendings", 0) or 0) > 0
    # One concurrent cycle per TF for hvn_inside_touch: an ACTIVE arm occupies the magic even
    # when MT5 momentarily reports flat (legs placed-but-unreported, or pendings mid-reanchor),
    # so a subsequent touch can't stack/replace a second straddle on the same node. Re-anchoring
    # while flat is owned by the poll-path _refresh_cycle_tps (MODIFY_PENDING) — no full re-arm
    # needed. Re-arm frees only once the cycle retires (active→False). Other setups keep the
    # active+flat re-arm fall-through below.
    if not force and arm.get("active") and not _live and plan.trigger_kind == "hvn_inside_touch":
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": "cycle_active(once_per_tf)",
                        "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf})
    if not force and arm.get("active") and _live:
        # Refresh TP + pending order prices from latest HVN edges while cycle is live
        # (positions OR resting pendings) — ON CANDLE CLOSE (this route fires per bar),
        # not on the 1s poll. HVNs shift intra-day; stale TPs/pending prices mis-target.
        try:
            from execution.zone_triggers import compute_hvn_tps, _session_hvn_zones
            from pipeline.features.vp_cache import get as vp_get
            # session HVNs (rolling today + cached prev-D) — same set the trigger armed on,
            # NOT daily-only (which omits the forming hvn_today node the entry sat on).
            _sbars = store().recent(symbol, tf, 120)
            _raw_zones, _ = _session_hvn_zones(symbol, tf, _sbars)
            if not _raw_zones:
                _dvp = vp_get(symbol, "daily") or {}
                _raw_zones = [(float(z["low"]), float(z["high"]))
                              for z in (_dvp.get("hvn_zones") or [])]
            if _raw_zones:
                # Work in raw Bybit frame; rebase only final results.
                # arm.fulcrum is venue-rebased → un-rebase for zone lookup.
                _ratio = float(quote.get("mid") or 0.0) / float(latest.ohlc.c) if latest.ohlc.c else 1.0
                _old_fulcrum_venue = float(arm.get("fulcrum") or 0.0)
                _edge_raw = _old_fulcrum_venue / _ratio if _ratio else _old_fulcrum_venue

                # Find new HVN edge: the edge of the zone nearest to the original fulcrum.
                # If the zone boundaries shifted, the edge moves → compute delta.
                _new_edge_raw = _edge_raw  # default: no shift
                _arm_edge = arm.get("edge", "")  # "top" or "bottom"
                _best_dist, _best_edge = float("inf"), _edge_raw
                for lo, hi in _raw_zones:
                    for candidate in (lo, hi):
                        d = abs(candidate - _edge_raw)
                        if d < _best_dist:
                            _best_dist, _best_edge = d, candidate
                # Only shift if the nearest edge moved more than 0.1 pts (noise floor)
                # and less than 5 pts (sanity cap — bigger = zone completely changed).
                _raw_delta = _best_edge - _edge_raw
                _venue_delta = round(_raw_delta * _ratio, 4)
                _modify_threshold = 0.1  # pts in venue frame

                if abs(_venue_delta) > _modify_threshold:
                    # Update stored fulcrum
                    _new_fulcrum_venue = round(_old_fulcrum_venue + _venue_delta, 4)
                    arm = {**arm, "fulcrum": _new_fulcrum_venue}
                    ExecBridge.set_last_arm(account, broker_symbol, **arm)
                    # Shift pending stop orders by the same delta
                    ExecBridge.enqueue_modify_pending(account, broker_symbol, leg_magic,
                                                      price_delta=_venue_delta)
                    _emit_audit({"account": account, "symbol": symbol, "tf": tf,
                                 "verdict": "fulcrum_shift", "magic": leg_magic,
                                 "old_fulcrum": _old_fulcrum_venue,
                                 "new_fulcrum": _new_fulcrum_venue,
                                 "delta": _venue_delta})

                # Recompute TPs from updated edge, with the min-distance floor. Pass the
                # reconstructed outermost legs so the TP clears the WHOLE ladder (matching
                # arm-time) — never regresses to an inside-ladder, un-profitable level.
                _min_tp = float((settings.get("grid_levels") or {}).get("min_tp_dist", 0.0) or 0.0)
                _min_tp_raw = _min_tp / _ratio if _ratio else _min_tp
                _buy_n_r  = int(arm.get("buy_n")  or arm.get("n_per_side") or 0)
                _sell_n_r = int(arm.get("sell_n") or arm.get("n_per_side") or 0)
                _stp_r = (float(arm.get("step") or 0.0) / _ratio) if _ratio else float(arm.get("step") or 0.0)
                _top_leg_r = _best_edge + _buy_n_r  * _stp_r if (_buy_n_r  > 0 and _stp_r > 0) else _best_edge
                _bot_leg_r = _best_edge - _sell_n_r * _stp_r if (_sell_n_r > 0 and _stp_r > 0) else _best_edge
                # hvn_inside_touch: target the NEXT node's far edge — exclude the node the
                # fulcrum sits in (arm-time bounds stored venue-frame → un-rebase via /_ratio).
                _skip_node = None
                if arm.get("trigger_kind") == "hvn_inside_touch" and _ratio:
                    _nlo_v = float(arm.get("node_low") or 0.0)
                    _nhi_v = float(arm.get("node_high") or 0.0)
                    if _nlo_v > 0 and _nhi_v > _nlo_v:
                        _skip_node = (_nlo_v / _ratio, _nhi_v / _ratio)
                # Unified TP rule (matches arm-time): next HVN far edge + VP refinements.
                from execution.zone_triggers import hvn_or_vp_tp as _hvn_or_vp_tp
                raw_tp_up, raw_tp_down = _hvn_or_vp_tp(symbol, _raw_zones, _top_leg_r, _bot_leg_r,
                                                       _stp_r, min_tp_dist=_min_tp_raw, skip_node=_skip_node)
                if raw_tp_up == 0.0 or raw_tp_down == 0.0:   # cascade only if nothing beyond
                    _cu, _cd = compute_hvn_tps(symbol, _best_edge, _raw_zones, skip_node=_skip_node,
                                               min_dist=_min_tp_raw, top_leg=_top_leg_r, bot_leg=_bot_leg_r)
                    if raw_tp_up   == 0.0: raw_tp_up   = _cu
                    if raw_tp_down == 0.0: raw_tp_down = _cd
                if not (raw_tp_up   and raw_tp_up   > _top_leg_r):     raw_tp_up   = 0.0
                if not (raw_tp_down and 0 < raw_tp_down < _bot_leg_r): raw_tp_down = 0.0
                new_tp_up   = round(raw_tp_up   * _ratio, 4) if raw_tp_up   else 0.0
                new_tp_down = round(raw_tp_down * _ratio, 4) if raw_tp_down else 0.0
                old_up   = float(arm.get("tp_up")   or 0.0)
                old_down = float(arm.get("tp_down") or 0.0)
                if (new_tp_up and abs(new_tp_up - old_up) > 0.05) or (new_tp_down and abs(new_tp_down - old_down) > 0.05):
                    ExecBridge.set_last_arm(account, broker_symbol,
                                            **{**arm, "tp_up": new_tp_up, "tp_down": new_tp_down})
                    # Update TP on remaining pending orders AND on already-filled positions
                    # (PositionModify keeps SL, swaps TP — so filled legs chase the same
                    # moving HVN target the pendings do, never stranded on a stale fill-time TP).
                    _open_buys  = int((open_state.get("buys")  or 0))
                    _open_sells = int((open_state.get("sells") or 0))
                    if new_tp_up:
                        ExecBridge.enqueue_modify_pending(account, broker_symbol, leg_magic,
                                                          price_delta=0.0, new_tp=new_tp_up, side="buy")
                        if _open_buys:
                            ExecBridge.enqueue_modify_position(account, broker_symbol, leg_magic,
                                                               new_tp=new_tp_up, side="buy",
                                                               comment="FB|tp_refresh|buy")
                    if new_tp_down:
                        ExecBridge.enqueue_modify_pending(account, broker_symbol, leg_magic,
                                                          price_delta=0.0, new_tp=new_tp_down, side="sell")
                        if _open_sells:
                            ExecBridge.enqueue_modify_position(account, broker_symbol, leg_magic,
                                                               new_tp=new_tp_down, side="sell",
                                                               comment="FB|tp_refresh|sell")
                    _emit_audit({"account": account, "symbol": symbol, "tf": tf,
                                 "verdict": "tp_refresh", "magic": leg_magic,
                                 "tp_up_old": old_up, "tp_down_old": old_down,
                                 "tp_up": new_tp_up, "tp_down": new_tp_down})
        except Exception:
            pass
        _os_buys  = int(open_state.get("buys",     0) or 0)
        _os_sells = int(open_state.get("sells",    0) or 0)
        _os_pend  = int(open_state.get("pendings", 0) or 0)
        _live_parts = []
        if _os_buys  > 0: _live_parts.append(f"{_os_buys}B")
        if _os_sells > 0: _live_parts.append(f"{_os_sells}S")
        if _os_pend  > 0: _live_parts.append(f"{_os_pend}pend")
        _live_str = ",".join(_live_parts) if _live_parts else "live"
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": f"cycle_live({_live_str})",
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
    # Each leg carries its own structural TP (HVN far-edge or POC). Per-leg TP is the
    # sole exit mechanism — no basket net_target. leg_tp always True.
    leg_tp = True
    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan,
                                        close_first=close_first, clear_kind="cancel",
                                        magic=leg_magic, leg_tp=leg_tp)
    edge = plan.trigger_context.get("edge", "")
    _ratio = (plan.venue_anchor / plan.analysis_anchor) if plan.analysis_anchor else 1.0
    node_low = float(plan.trigger_context.get("node_low", 0.0) or 0.0) * _ratio
    node_high = float(plan.trigger_context.get("node_high", 0.0) or 0.0) * _ratio
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=edge,
                            trigger_kind=plan.trigger_kind, venue_mid=quote["mid"], magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            bias_peak=0.0, bias_booked=False,
                            be_done_buy=False, be_done_sell=False,
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
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


@bp.post("/exec/refresh_tps")
def exec_refresh_tps():
    """Refresh entry prices + TPs for EVERY active cycle (any TF) against the live HVN.
    Called once per 1m bar close (by the emitter) so all orders re-target structure at a
    uniform 1m cadence regardless of which TF armed them. Per-cycle no-op if nothing moved.

    Body: {account, symbol(broker or analysis)}."""
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]
    if not account:
        return jsonify({"ok": False, "error": "missing account"}), 400

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    b2a = {v: k for k, v in symbol_map.items()}
    broker_symbol = symbol_map.get(req_symbol, req_symbol)  # accept analysis OR broker
    if req_symbol in b2a:                                   # was already broker
        broker_symbol = req_symbol
    analysis_sym = b2a.get(broker_symbol, broker_symbol)

    refreshed = []
    for mg in ExecBridge.active_fulcrums(account, broker_symbol).keys():
        tf_m = tf_from_magic(mg)
        if not tf_m:
            continue
        try:
            _refresh_cycle_tps(account, broker_symbol, analysis_sym, tf_m, mg, settings)
            refreshed.append(mg)
        except Exception:
            LOG.exception(f"[refresh_tps] magic={mg} error")
    return jsonify({"ok": True, "account": account, "broker_symbol": broker_symbol,
                    "refreshed_magics": refreshed})


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

    # Fetch the EA quote first — used to rebase analysis-frame (Bybit) zones onto the
    # broker venue price so zone rectangles, levels, histogram and fulcrum all align.
    quote = ExecBridge.get_quote(account, broker_symbol) or {}

    from pipeline.features import vp_cache
    from pipeline.state_store import store as _store
    # Fetch prev-D and today separately so we can overlay both on the chart.
    # /exec/zones always returns both: prev-D zones as "hvn"/"lvn", today's as
    # "hvn_today"/"lvn_today" so the EA can color them differently.
    _prev_daily, _today_daily = vp_cache.get_prev_and_today(symbol)
    # Fallback: if neither exists use the standard get() (weekly or legacy path).
    daily = _prev_daily or _today_daily or vp_cache.get(symbol, "daily") or {}

    # Zone rebase: when MetaAPI offset is 0 (403 plan) derive the ratio from the live
    # EA quote vs Bybit last close so EA-drawn zones align with the rebased fulcrum.
    _zone_ratio = 1.0
    _broker_mid = float(quote.get("mid") or 0.0)
    if _broker_mid > 0:
        _bybit_bars = _store().recent(symbol, "1m", 2)
        _bybit_close = float(_bybit_bars[-1].ohlc.c) if _bybit_bars else 0.0
        if _bybit_close > 0:
            _zone_ratio = _broker_mid / _bybit_close

    def _rebase_price(p: float) -> float:
        return round(p * _zone_ratio, 5) if _zone_ratio != 1.0 else round(p, 5)

    zones = []
    # Prev-D zones (completed session reference) — drawn with standard HVN/LVN colors.
    for z in (_prev_daily.get("hvn_zones") or [] if _prev_daily else []):
        zones.append({"kind": "hvn", "lo": _rebase_price(float(z["low"])),
                      "hi": _rebase_price(float(z["high"]))})
    for z in (_prev_daily.get("lvn_zones") or [] if _prev_daily else []):
        zones.append({"kind": "lvn", "lo": _rebase_price(float(z["low"])),
                      "hi": _rebase_price(float(z["high"]))})
    # Today's forming zones — drawn with "_today" kinds so the EA colors them distinctly.
    for z in (_today_daily.get("hvn_zones") or [] if _today_daily else []):
        zones.append({"kind": "hvn_today", "lo": _rebase_price(float(z["low"])),
                      "hi": _rebase_price(float(z["high"]))})
    for z in (_today_daily.get("lvn_zones") or [] if _today_daily else []):
        zones.append({"kind": "lvn_today", "lo": _rebase_price(float(z["low"])),
                      "hi": _rebase_price(float(z["high"]))})

    # VP point-levels the grid actually TRIGGERS on (vp_level_touch fulcrums), drawn as
    # labeled lines — only the levels enabled in grid_levels.vp_fulcrum_levels. Same
    # venue-shifted daily VP, so they line up with the zones above and the dashboard.
    enabled = set((settings.get("grid_levels") or {}).get("vp_fulcrum_levels", []) or [])
    levels = []
    for k in ("poc", "vah", "val", "naked_poc"):
        if k in enabled:
            v = daily.get(k)
            if isinstance(v, (int, float)) and v > 0:
                levels.append({"kind": k, "price": _rebase_price(float(v))})
    # Today's forming session POC + value area (VAH/VAL) as distinct levels so the user
    # can see the developing session value, separate from the prev-D vah/val above.
    if _today_daily:
        for _src_k, _out_k in (("poc", "poc_today"), ("vah", "vah_today"), ("val", "val_today")):
            _v = _today_daily.get(_src_k)
            if isinstance(_v, (int, float)) and _v > 0:
                levels.append({"kind": _out_k, "price": _rebase_price(float(_v))})

    # Computed volume-at-price histogram (venue-shifted) for the EA to draw as a sideways
    # profile. Rebuilt from bars (cache keeps only aggregates). Same daily window as zones.
    prof = vp_cache.period_profile(symbol, "daily") or {}
    profile = [{"price": _rebase_price(float(b["price"])), "vol": b["vol"]}
               for b in prof.get("profile", [])]
    vp_bin = round(float(prof.get("bin", 0.0)) * _zone_ratio, 5) if prof.get("bin") else 0.0
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

    # Touch-trigger lines: the exact prices where a LIVE tap would arm an entry =
    # each HVN edge ± hvn_touch_buffer (a tap within the buffer of an edge triggers).
    # Drawn green-dotted by the EA so you can see where price needs to reach. Only the
    # touch-armed TFs get them (lines elsewhere would imply an entry that won't fire).
    grid_cfg2 = settings.get("grid_levels") or {}
    touch_lines = []
    if bool(grid_cfg2.get("touch_arm_enabled", False)):
        _buf = float(grid_cfg2.get("hvn_touch_buffer", 0.0) or 0.0) * _zone_ratio
        for z in zones:
            if z["kind"] != "hvn":
                continue
            # top edge: a tap at hi-buf or above fires → line at hi-buf
            # bottom edge: a tap at lo+buf or below fires → line at lo+buf
            touch_lines.append({"price": round(z["hi"] - _buf, 5), "side": "top"})
            touch_lines.append({"price": round(z["lo"] + _buf, 5), "side": "bottom"})

    # HVN → cycle map: for each HVN zone, list which active cycles are anchored inside it.
    # A cycle belongs to an HVN if its venue-frame fulcrum sits within [lo, hi] (with a
    # ±tol band equal to hvn_touch_buffer so edge-touching cycles are included).
    _touch_buf = float((settings.get("grid_levels") or {}).get("hvn_touch_buffer", 0.0) or 0.0) * _zone_ratio
    _active_cycles = ExecBridge.active_cycles_detail(account, broker_symbol)
    hvn_cycle_map = []
    for z in zones:
        if z["kind"] != "hvn":
            continue
        lo, hi = z["lo"], z["hi"]
        tol = _touch_buf or (hi - lo) * 0.1  # fallback: 10% of zone width
        matched = [
            c for c in _active_cycles
            if lo - tol <= c["fulcrum"] <= hi + tol
        ]
        hvn_cycle_map.append({
            "lo": lo, "hi": hi,
            "cycles": matched,
        })

    # CVD divergence signals — scan recent bars on the zone TF so the EA can mark
    # divergence candles with arrows. Looks back `cvd_lookback` bars (default 50) for
    # any bar where price broke the prior-window extreme but delta lagged.
    cvd_signals = []
    try:
        from pipeline.features.delta_divergence import detect as _cvd_detect
        from pipeline.state_store import store as _cvd_store
        _cvd_tf = zone_tf or "15m"
        _cvd_window = 5
        _cvd_lookback = 50
        # TF → seconds so we can compute bar OPEN time (MT5 bars are keyed by open time).
        _tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                    "1d": 86400}.get(_cvd_tf, 900)
        _cvd_bars = _cvd_store().recent(symbol, _cvd_tf, _cvd_lookback + _cvd_window + 2)
        for _i in range(_cvd_window, len(_cvd_bars)):
            _bar = _cvd_bars[_i]
            _hist = _cvd_bars[max(0, _i - _cvd_window):_i]
            _dd = _cvd_detect(_bar, _hist, window=_cvd_window)
            if _dd.fired:
                # price anchor: bearish → candle high (price made new high, delta failed)
                #               bullish → candle low (price made new low, delta failed)
                _px = _bar.ohlc.h if _dd.direction == "bearish" else _bar.ohlc.l
                # MT5 places objects by bar OPEN time; close_ts - tf_secs = open_ts.
                _open_ts = int(_bar.close_ts) - _tf_secs
                cvd_signals.append({
                    "bar_time": _open_ts,
                    "price": _rebase_price(float(_px)),
                    "direction": _dd.direction,
                })
    except Exception:
        pass

    # Armed-node overlay: the EXACT node each active cycle triggered on (node_low..node_high
    # + fulcrum), across ALL TFs — already venue-frame (rebased at emit). The drawn HVN/LVN
    # zones are the DAILY VP, but a grid arms on its per-TF ROLLING VP, whose edges can differ
    # (esp. 1m). Drawing the armed node makes the touched edge visible regardless of InpZoneTF.
    armed_nodes = [c for c in _active_cycles
                   if float(c.get("node_low") or 0.0) > 0 and float(c.get("node_high") or 0.0) > 0]

    return jsonify({"ok": True, "zones": zones, "levels": levels, "ict": ict_out,
                    "profile": profile, "vp_bin": vp_bin,
                    "touch_lines": touch_lines,
                    "hvn_cycle_map": hvn_cycle_map,
                    "armed_nodes": armed_nodes,
                    "cvd_signals": cvd_signals,
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


@bp.post("/exec/close_magic")
def exec_close_magic():
    """Enqueue CLOSE_ALL for specific magics — closes positions + cancels pendings on the EA.
    Body: {account, symbol, magics: [int, ...]}"""
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    symbol = str(body.get("symbol") or "")
    magics = body.get("magics") or []
    if not account or not symbol or not magics:
        return jsonify({"ok": False, "error": "need account, symbol, magics"}), 400
    for mg in magics:
        ExecBridge.enqueue(account, "CLOSE_ALL", symbol, magic=int(mg),
                           comment="FB|force_close")
    LOG.info(f"[exec] close_magic queued CLOSE_ALL for {magics} on {account}/{symbol}")
    return jsonify({"ok": True, "queued": [int(m) for m in magics]})


@bp.post("/exec/retire_cycle")
def exec_retire_cycle():
    """Force-retire one or more stale cycles that the EA already flattened manually.
    Body: {account, symbol, magics: [int, ...]}  — marks each magic active=False."""
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    symbol = str(body.get("symbol") or "")
    magics = body.get("magics") or []
    if not account or not symbol or not magics:
        return jsonify({"ok": False, "error": "need account, symbol, magics"}), 400
    retired = []
    for mg in magics:
        cyc = ExecBridge.get_last_arm(account, symbol, magic=int(mg))
        if cyc:
            # explicit magic: persisted arms may lack a `magic` key (would default the
            # set_last_arm key to 0 and leave the real entry active=True).
            cyc.pop("magic", None)
            ExecBridge.set_last_arm(account, symbol, magic=int(mg), **{**cyc, "active": False})
        else:
            ExecBridge.set_last_arm(account, symbol, magic=int(mg), active=False,
                                    tf="", fulcrum=0.0, edge="", trigger_kind="",
                                    venue_mid=0.0, n_per_side=0, step=0.0,
                                    buy_n=0, sell_n=0, bias_peak=0.0, bias_booked=False,
                                    be_done_buy=False, be_done_sell=False,
                                    node_low=0.0, node_high=0.0,
                                    max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                                    squeeze_ok=False, squeeze_rank=0.0, armed_tf="",
                                    tp_up=0.0, tp_down=0.0)
        # Sweep any residual resting pendings on this magic — else the EA keeps
        # reporting them in magics[], which re-activates the cycle every poll.
        ExecBridge.enqueue(account, "CANCEL_PENDINGS", symbol, magic=int(mg),
                           comment="FB|retire")
        retired.append(int(mg))
        ExecBridge.clear_emit(account, symbol, magic=int(mg))
    LOG.info(f"[exec] retire_cycle account={account} symbol={symbol} magics={retired}")
    return jsonify({"ok": True, "retired": retired})


@bp.post("/exec/fix_tps")
def exec_fix_tps():
    """Recompute VP-structural TPs for all live pending orders and enqueue MODIFY_PENDING.

    Body: {"account": "...", "pending_orders": [{ticket, magic, type, price, tp}, ...]}
    The EA should POST its current pending order list; this endpoint diffs old vs new TP
    and enqueues corrections for any order whose TP deviates by more than 0.05 pts.
    """
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    account = str(body.get("account", ""))
    pending = body.get("pending_orders", [])
    if not account:
        return jsonify({"ok": False, "error": "account and pending_orders required"})

    from execution.exec_bridge import MODIFY_PENDING
    from execution.zone_triggers import compute_hvn_tps
    from pipeline.features.vp_cache import get as vp_get

    fixed = 0
    skipped = 0
    for order in pending:
        magic       = int(order.get("magic", 0))
        ticket      = int(order.get("ticket", 0))
        order_type  = str(order.get("type", ""))
        price       = float(order.get("price", 0.0))
        old_tp      = float(order.get("tp", 0.0))
        broker_sym  = str(order.get("symbol", ""))

        arm = ExecBridge.get_last_arm(account, broker_sym, magic) or {}
        # prefer stored analysis symbol; fall back via settings symbol_map
        if not arm.get("symbol"):
            import yaml as _yaml
            from pathlib import Path as _Path
            _settings = _yaml.safe_load(
                (_Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml").read_text()
            ) or {}
            _sym_map = (_settings.get("execution") or {}).get("symbol_map") or {}
            _broker_to = {v: k for k, v in _sym_map.items()}
            arm["symbol"] = _broker_to.get(broker_sym, broker_sym)
        analysis_sym = arm.get("symbol", broker_sym)

        # hvn_inside_touch: use the SAME session zones the trigger armed on (not daily) and
        # target the NEXT node beyond the touched node, so this repair can't re-clobber the
        # TP back to the node price already sits in. Every other setup keeps daily zones.
        _is_inside = arm.get("trigger_kind") == "hvn_inside_touch"
        hvn_zones = []
        if _is_inside:
            try:
                from execution.zone_triggers import _session_hvn_zones
                from pipeline.state_store import store as _store
                _tf = str(arm.get("armed_tf") or arm.get("tf") or "")
                if _tf:
                    _sb = _store().recent(analysis_sym, _tf, 120)
                    _sz, _ = _session_hvn_zones(analysis_sym, _tf, _sb)
                    hvn_zones = [(float(lo), float(hi)) for lo, hi in (_sz or [])]
            except Exception:
                hvn_zones = []
        if not hvn_zones:
            dvp = vp_get(analysis_sym, "daily") or {}
            hvn_zones = [(float(z["low"]), float(z["high"]))
                         for z in (dvp.get("hvn_zones") or [])]

        if not hvn_zones:
            skipped += 1
            continue

        # rebase price back to analysis frame for compute_hvn_tps
        venue_mid = float(arm.get("venue_mid", 0.0) or 0.0)
        bybit_mid = float(arm.get("bybit_mid", 0.0) or 0.0)
        ratio = (bybit_mid / venue_mid) if venue_mid > 0 and bybit_mid > 0 else 1.0
        analysis_price = price * ratio

        # skip_node = touched node bounds (stored venue-frame → analysis via *ratio here,
        # since analysis = venue * ratio in this endpoint's frame convention).
        _skip_node = None
        if _is_inside:
            _nlo_v = float(arm.get("node_low") or 0.0)
            _nhi_v = float(arm.get("node_high") or 0.0)
            if _nlo_v > 0 and _nhi_v > _nlo_v:
                _skip_node = (_nlo_v * ratio, _nhi_v * ratio)
        tp_up, tp_down = compute_hvn_tps(analysis_sym, analysis_price, hvn_zones, skip_node=_skip_node)

        # pick which TP applies to this order side
        is_buy = "buy" in order_type.lower()
        new_tp_raw = tp_up if is_buy else tp_down

        # rebase back to venue frame
        new_tp = round(new_tp_raw / ratio, 4) if ratio != 1.0 and new_tp_raw > 0 else round(new_tp_raw, 4)

        if new_tp <= 0:
            skipped += 1
            continue

        if abs(new_tp - old_tp) < 0.05:
            skipped += 1
            continue

        ExecBridge.enqueue(account, MODIFY_PENDING, broker_sym,
                           magic=magic, price=0.0, tp=new_tp,
                           side="buy" if is_buy else "sell")
        LOG.info(f"[fix_tps] magic={magic} ticket={ticket} {order_type} "
                 f"old_tp={old_tp} → new_tp={new_tp}")
        fixed += 1

    return jsonify({"ok": True, "fixed": fixed, "skipped": skipped})
