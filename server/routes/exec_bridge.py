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

from execution.exec_bridge import ExecBridge

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
        ExecBridge.set_quote(account, sym, float(bid), float(ask))
    if sym and ("positions" in body or "pendings" in body):
        ExecBridge.set_open(account, sym, int(body.get("positions", 0)),
                            int(body.get("pendings", 0)))
    # Cycle monitor: decide exits on THIS poll's fresh quote/open-state so a flatten
    # ships in the same response (saves a ~1s round-trip). Runs BEFORE poll(). Gated on
    # field presence → an older EA binary (no pnl/buys/sells) just skips net-$/full-hedge.
    if sym:
        buys = int(body["buys"]) if "buys" in body else None
        sells = int(body["sells"]) if "sells" in body else None
        pnl = float(body["pnl"]) if "pnl" in body else None
        try:
            ExecBridge.monitor_cycle(account, sym, current_app.config.get("FB_SETTINGS"),
                                     pnl=pnl, buys=buys, sells=sells)
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
    trigger_hint = str(body.get("trigger_hint") or "hvn_inside_touch")
    close_first = bool(body.get("close_first", True))
    if not account:
        return jsonify({"ok": False, "error": "missing account"}), 400

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
    from execution.grid_planner import plan_grid_levels
    latest = store().latest(symbol, tf)
    if latest is None:
        return jsonify({"ok": False, "verdict": "skip",
                        "skip_reason": f"no bars stored for {symbol} {tf}"}), 404

    plan = plan_grid_levels(symbol, tf, float(latest.ohlc.c),
                            trigger_hint=trigger_hint, settings=settings,
                            venue_price=float(quote["mid"]))
    if plan.verdict != "arm":
        # episode ended → next arm on this symbol/tf is a fresh touch
        ExecBridge.clear_emit(account, symbol, tf)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": plan.skip_reason})
        return jsonify({"ok": True, "verdict": "skip", "skip_reason": plan.skip_reason,
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Strict: plan_grid_levels falls back to ALL triggers when the hinted one is
    # absent — but emit must fire ONLY the requested strategy, never a stray
    # hvn_edge/va/etc. grid. Mismatch → treat as no-arm.
    if trigger_hint and plan.trigger_kind != trigger_hint:
        ExecBridge.clear_emit(account, symbol, tf)
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": f"trigger_mismatch:{plan.trigger_kind}"})
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": f"no_{trigger_hint}_trigger (got {plan.trigger_kind})",
                        "symbol": symbol, "broker_symbol": broker_symbol})

    # Cycle-ownership gate: exactly ONE active cycle per (account, broker_symbol).
    # There is a single position pool per (symbol, magic) — the EA can't attribute a
    # fill to 5m vs 15m — so overlapping cycles are incoherent. Skip if a cycle is
    # active and owned by a DIFFERENT tf. The owning tf may refresh ONLY while flat
    # (re-place the straddle at the newest edge). This replaces the stale-open-state
    # positions>0 gate that let a re-arm flatten freshly-filled positions.
    arm = ExecBridge.get_last_arm(account, broker_symbol) or {}
    open_state = ExecBridge.get_open(account, broker_symbol)
    force = bool(body.get("force", False))
    if not force and arm.get("active"):
        owner_tf = arm.get("armed_tf", "")
        if owner_tf and owner_tf != tf:
            return jsonify({"ok": True, "verdict": "skip", "skip_reason": f"cycle_owned_by:{owner_tf}",
                            "symbol": symbol, "broker_symbol": broker_symbol})
        if open_state.get("positions", 0) > 0:
            return jsonify({"ok": True, "verdict": "skip", "skip_reason": "position_open",
                            "symbol": symbol, "broker_symbol": broker_symbol, "open": open_state})
        # same tf, flat → fall through and refresh the straddle at the new edge

    # Re-arm clears stale pendings with CANCEL_PENDINGS (never CLOSE_ALL) so it can't
    # flatten a live position. The deliberate flatten is owned solely by monitor_cycle.
    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan,
                                        close_first=close_first, clear_kind="cancel")
    edge = plan.trigger_context.get("edge", "")
    net_target = float(((settings.get("grid_levels") or {}).get("cycle_net_target_usd", 0.0)) or 0.0)
    # ground truth + cycle state: touched edge (=fulcrum), TF owner, structural targets
    # (tp_up=buy target, tp_down=sell target), and the exit-monitor bookkeeping fields.
    ExecBridge.set_last_arm(account, broker_symbol, fulcrum=plan.fulcrum, tf=tf, edge=edge,
                            trigger_kind=plan.trigger_kind, venue_mid=quote["mid"],
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            net_target_usd=net_target, max_pos_seen=0, pend_seen=0, flatten_ts=0.0)
    _emit_audit({"account": account, "symbol": symbol, "broker_symbol": broker_symbol,
                 "tf": tf, "verdict": "arm", "trigger_kind": plan.trigger_kind, "edge": edge,
                 "fulcrum": plan.fulcrum, "venue_mid": quote["mid"],
                 "analysis_anchor": plan.analysis_anchor, "n_per_side": plan.n_per_side,
                 "step": plan.step,
                 "buy_legs": [l.price for l in plan.buy_legs],
                 "sell_legs": [l.price for l in plan.sell_legs],
                 "buy_tp": plan.buy_tp, "sell_tp": plan.sell_tp})
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
    """Current rolling-VP HVN/LVN zones for symbol/tf, rebased onto the account's
    cached venue price, for the EA to draw on the chart.

    Body: {account, symbol(broker or analysis), tf}.
    Returns {ok, zones:[{kind:"hvn"|"lvn", lo, hi}], venue_mid}. Empty if no quote
    or no profile yet.
    """
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    account = str(body.get("account") or "")
    req_symbol = body.get("symbol") or settings["instrument"]["symbol"]
    tf = body.get("tf") or "15m"

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    symbol = broker_to_analysis.get(req_symbol, req_symbol)
    broker_symbol = symbol_map.get(symbol, req_symbol)

    quote = ExecBridge.get_quote(account, broker_symbol)
    if not quote:
        return jsonify({"ok": True, "zones": [], "reason": "no venue quote cached"})

    from pipeline.state_store import store
    from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
    _VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}
    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win)
    if len(bars) < 20:
        return jsonify({"ok": True, "zones": [], "reason": "not enough bars"})

    analysis_anchor = float(bars[-1].ohlc.c)
    ratio = float(quote["mid"]) / analysis_anchor if analysis_anchor > 0 else 1.0
    try:
        vp = vp_compute(bars, "daily", analysis_anchor, bin_size=DEFAULT_BIN_SIZE.get(symbol))
    except Exception as e:
        return jsonify({"ok": True, "zones": [], "reason": f"vp_error:{e}"})

    zones = []
    for z in (vp.hvn_zones or []):
        zones.append({"kind": "hvn", "lo": round(float(z["low"]) * ratio, 5),
                      "hi": round(float(z["high"]) * ratio, 5)})
    for z in (vp.lvn_zones or []):
        zones.append({"kind": "lvn", "lo": round(float(z["low"]) * ratio, 5),
                      "hi": round(float(z["high"]) * ratio, 5)})
    # surface the last-armed grid so the EA can draw the fulcrum (touched edge)
    arm = ExecBridge.get_last_arm(account, broker_symbol) or {}
    return jsonify({"ok": True, "zones": zones, "venue_mid": quote["mid"],
                    "symbol": symbol, "broker_symbol": broker_symbol,
                    "fulcrum": arm.get("fulcrum", 0.0), "emit_tf": arm.get("tf", ""),
                    "emit_edge": arm.get("edge", "")})


@bp.get("/exec/queue")
def exec_queue():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    account = request.args.get("account")
    return jsonify({"ok": True, "commands": ExecBridge.snapshot(account),
                    "last_poll_body": getattr(ExecBridge, "last_poll_body", None)})
