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


def _trigger_entry(grid_cfg: dict, kind: str) -> dict | None:
    """Return the trigger config dict for `kind` if enabled, else None."""
    for entry in (grid_cfg.get("triggers") or []):
        if isinstance(entry, str):
            if entry == kind:
                return {"kind": kind}
        elif isinstance(entry, dict) and entry.get("kind") == kind:
            if entry.get("enabled", True):
                return entry
            return None
    return None


def _trigger_tfs(grid_cfg: dict, kind: str) -> list:
    """Return the list of TFs for which `kind` is active."""
    entry = _trigger_entry(grid_cfg, kind)
    if entry is None:
        return []
    tfs = entry.get("tfs")
    return list(tfs) if tfs else ["1m", "5m", "15m", "1h"]


def _refresh_cycle_tps(account: str, broker_symbol: str, analysis_symbol: str,
                       tf: str, magic: int, settings: dict,
                       include_positions: bool = False) -> None:
    """Track the moving HVN for ONE active cycle (called each poll). Recompute the
    fulcrum edge + structural TPs from the latest daily HVN zones; if they shifted,
    MODIFY_PENDING (entry prices + TP) and MODIFY_POSITION (filled-leg TP, keep SL) so
    BOTH pending and open orders chase the live HVN — never stranded on arm-time values.

    Works for pending-only cycles (no fill yet) and filled cycles alike. No-op unless the
    cycle is active and the recomputed edge/TP actually moved beyond the noise floor.

    include_positions: the 1s poll only touches pendings; the bar-close caller passes
    True so filled-leg TPs are also refreshed (MODIFY_POSITION, SL preserved)."""
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
    _gcfg = settings.get("grid_levels") or {}
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
            # FADED candidate — anchor node absent THIS recompute. Zone recomputes are
            # noisy (rolling-VP jitter flickers borderline nodes in/out), so require
            # hvn_fade_confirm_n wall-clock-spaced strikes before flattening; any check
            # that finds the anchor bracketed / within drift cap resets the count.
            _confirm_n = int(_gcfg.get("hvn_fade_confirm_n", 3) or 3)
            _strike_gap = float(_gcfg.get("hvn_fade_strike_min_gap_s", 240) or 240)
            _strikes = int(arm.get("fade_strikes") or 0)
            _now = _t.time()
            if _now - float(arm.get("fade_last_strike_ts") or 0.0) >= _strike_gap:
                _strikes += 1
                arm = {**arm, "fade_strikes": _strikes, "fade_last_strike_ts": _now}
                ExecBridge.set_last_arm(account, broker_symbol, **arm)
                _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                             "verdict": "hvn_fade_strike", "magic": magic, "poll": True,
                             "strikes": _strikes, "confirm_n": _confirm_n,
                             "old_fulcrum": old_fulcrum_venue,
                             "nearest_edge_dist": round(best_dist, 4)})
            if _strikes < _confirm_n:
                return   # suspect edge — don't chase it (skip shift/TP this check)
            # CONFIRMED FADED — anchor HVN dissolved before any fill. Premise gone → flatten.
            ExecBridge.enqueue(account, "CLOSE_ALL", broker_symbol, magic=magic,
                               comment="FB|hvn_faded")
            _cyc = ExecBridge.get_last_arm(account, broker_symbol, magic=magic) or {}
            _cyc.pop("magic", None)
            ExecBridge.set_last_arm(account, broker_symbol, magic=magic,
                                    **{**_cyc, "active": False, "flatten_ts": __import__("time").time()})
            ExecBridge.clear_emit(account, broker_symbol, magic=magic)
            _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                         "verdict": "hvn_faded_flatten", "magic": magic, "poll": True,
                         "exit_reason": "hvn_faded", "strikes": _strikes,
                         "old_fulcrum": old_fulcrum_venue, "nearest_edge_dist": round(best_dist, 4)})
            return   # cycle retired — nothing more to refresh
        elif int(arm.get("fade_strikes") or 0):
            # anchor back (bracketed or within drift cap) → clear accrued strikes
            arm = {**arm, "fade_strikes": 0}
            ExecBridge.set_last_arm(account, broker_symbol, **arm)

        # DRIFT or RE-ANCHOR: shift fulcrum + pendings to best_edge (still on real structure).
        # DEFER, don't drop: advance the persisted fulcrum ONLY when the broker MODIFY actually
        # fired. If throttled (modify_cooldown_s not yet elapsed → enqueue returns None), leave
        # the fulcrum at the last APPLIED edge so the FULL accumulated delta re-applies on the
        # next poll past the cooldown — pendings re-track the HVN once per modify_cooldown_s
        # interval instead of having mid-cooldown drift advance state but never reach the broker.
        # Noise floor is structure-relative: sub-step edge wiggle (bin-level VP jitter)
        # must not generate broker modifies — that modify rate is what Vantage flagged.
        _shift_floor = max(float(_gcfg.get("hvn_shift_min_frac_step", 0.25) or 0.25)
                           * _step_venue, 0.1)
        if abs(venue_delta) > _shift_floor:
            _kind = "fulcrum_shift" if best_dist <= _drift_cap else "fulcrum_reanchor"
            new_fulcrum_venue = round(old_fulcrum_venue + venue_delta, 4)
            if ExecBridge.enqueue_modify_pending(account, broker_symbol, magic,
                                                 price_delta=venue_delta) is not None:
                arm = {**arm, "fulcrum": new_fulcrum_venue}
                ExecBridge.set_last_arm(account, broker_symbol, **arm)
                _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                             "verdict": _kind, "magic": magic, "poll": True,
                             "old_fulcrum": old_fulcrum_venue, "new_fulcrum": new_fulcrum_venue,
                             "delta": venue_delta, "bracketed": _bracketed})
            else:
                # throttled → pendings are still at the un-shifted edge; compute TPs from THERE
                # (not the new edge) so the structural TP stays consistent with the live orders.
                best_edge = edge_raw

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
    from execution.zone_triggers import bb_tp as _bb_tp
    _bb_bars = store().recent(analysis_symbol, tf, 25)
    _bb_up, _bb_dn = _bb_tp(_bb_bars, cfg=settings.get("grid_levels") or {})
    if _bb_up > _top_leg:
        raw_tp_up   = _bb_up if raw_tp_up   == 0.0 else min(raw_tp_up,   _bb_up)
    if 0 < _bb_dn < _bot_leg:
        raw_tp_down = _bb_dn if raw_tp_down == 0.0 else max(raw_tp_down, _bb_dn)
    # Runtime expansion upgrade: if expansion fires on a cycle that wasn't squeeze_ok at arm
    # time, flip squeeze_ok=True so the 2x target/trail multipliers activate immediately.
    # Condition: squeeze fully released (BBW not in/near compression) AND ATR expanding.
    if not arm.get("squeeze_ok"):
        from execution.zone_triggers import (squeeze_gate as _sq_gate,
                                             session_atr_ratio as _sess_atr)
        _grid_cfg = settings.get("grid_levels") or {}
        _exp_thresh = float(_grid_cfg.get("expansion_atr_ratio", 1.5) or 1.5)
        _atr_ratio = _sess_atr(_bb_bars)
        if _atr_ratio >= _exp_thresh:
            _sq_ok_now, _ = _sq_gate(analysis_symbol, tf, _grid_cfg)
            if not _sq_ok_now:  # BBW fully expanded — not in or near compression
                arm["squeeze_ok"] = True
                arm["squeeze_expansion"] = True  # marks runtime upgrade vs arm-time coil
                ExecBridge.set_last_arm(account, broker_symbol, **arm)
                _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                             "verdict": "squeeze_expansion_upgrade", "magic": magic,
                             "atr_ratio": round(_atr_ratio, 3)})
    # Guard: TP must lie strictly beyond the outermost leg or the grid can't profit on that
    # side — drop it to 0 (leave the existing TP untouched) rather than set an inside-ladder TP.
    if not (raw_tp_up   and raw_tp_up   > _top_leg):       raw_tp_up   = 0.0
    if not (raw_tp_down and 0 < raw_tp_down < _bot_leg):   raw_tp_down = 0.0
    new_tp_up = round(raw_tp_up * ratio, 4) if raw_tp_up else 0.0
    new_tp_down = round(raw_tp_down * ratio, 4) if raw_tp_down else 0.0
    old_up = float(arm.get("tp_up") or 0.0)
    old_down = float(arm.get("tp_down") or 0.0)
    # Structure-relative TP noise floor (same rationale as the fulcrum-shift floor).
    _tp_floor = max(float(settings.get("grid_levels", {}).get("tp_refresh_min_frac_step", 0.25)
                          or 0.25) * float(arm.get("step") or 0.0), 0.05)
    if (new_tp_up and abs(new_tp_up - old_up) > _tp_floor) or (new_tp_down and abs(new_tp_down - old_down) > _tp_floor):
        # DEFER, don't drop (same rule as the fulcrum shift): persist a side's new TP ONLY when
        # its MODIFY actually fired. A throttled side keeps its OLD tp_up/tp_down so the refresh
        # re-attempts each poll until modify_cooldown_s elapses — TP re-tracks the HVN once per
        # interval rather than recording a new value that never reached the broker. buy/sell are
        # separate throttle slots, so each side defers independently.
        _tp_arm = dict(arm)
        _tp_changed = False
        # filled-leg TP refresh: bar-close caller only (include_positions), never 1s poll
        _arm_sl_buy  = float(arm.get("sl_buy")  or arm.get("trail_sl_buy")  or 0.0)
        _arm_sl_sell = float(arm.get("sl_sell") or arm.get("trail_sl_sell") or 0.0)
        if new_tp_up and abs(new_tp_up - old_up) > _tp_floor and ExecBridge.enqueue_modify_pending(
                account, broker_symbol, magic, price_delta=0.0, new_tp=new_tp_up, side="buy") is not None:
            _tp_arm["tp_up"] = new_tp_up
            _tp_changed = True
            if include_positions and _open_buys:
                ExecBridge.enqueue_modify_position(account, broker_symbol, magic,
                                                   new_tp=new_tp_up, side="buy",
                                                   comment="FB|tp_refresh|buy", sl=_arm_sl_buy)
        if new_tp_down and abs(new_tp_down - old_down) > _tp_floor and ExecBridge.enqueue_modify_pending(
                account, broker_symbol, magic, price_delta=0.0, new_tp=new_tp_down, side="sell") is not None:
            _tp_arm["tp_down"] = new_tp_down
            _tp_changed = True
            if include_positions and _open_sells:
                ExecBridge.enqueue_modify_position(account, broker_symbol, magic,
                                                   new_tp=new_tp_down, side="sell",
                                                   comment="FB|tp_refresh|sell", sl=_arm_sl_sell)
        if _tp_changed:
            ExecBridge.set_last_arm(account, broker_symbol, **_tp_arm)
            _emit_audit({"account": account, "symbol": analysis_symbol, "tf": tf,
                         "verdict": "tp_refresh", "magic": magic, "poll": True,
                         "tp_up_old": old_up, "tp_down_old": old_down,
                         "tp_up": _tp_arm.get("tp_up", old_up), "tp_down": _tp_arm.get("tp_down", old_down)})


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


def _touch_arm_tf(account: str, broker_symbol: str, tf: str, settings: dict,
                  venue_mid: float | None = None) -> None:
    """Intrabar touch-arm for ONE touch-enabled TF, called each poll. Resolves the
    HVN edge live price is tapping, runs the tick-reversal confirm, and on confirm
    arms the same straddle the close-driven emit would — using the live edge as the
    fulcrum (no candle-close wait). Server stays the brain; the EA only reports price.

    No-ops unless: hvn_inside_touch is in the triggers list for this TF, a venue
    quote is cached, the symbol is flat on this magic, and the fulcrum is a NEW
    episode (dedup).

    venue_mid: live bid/ask midpoint from the EA's poll body. When provided this is
    the actual intrabar price, not the last bar close — essential for catching edge
    taps that happen mid-bar and resolve before bar close."""
    from execution.grid_planner import plan_grid_levels
    from execution.zone_triggers import touch_arm_trigger
    grid_cfg = settings.get("grid_levels") or {}
    if tf not in _trigger_tfs(grid_cfg, "hvn_inside_touch"):
        return

    symbol_map = (settings.get("execution") or {}).get("symbol_map") or {}
    broker_to_analysis = {v: k for k, v in symbol_map.items()}
    analysis = broker_to_analysis.get(broker_symbol, broker_symbol)

    quote = ExecBridge.get_quote(account, broker_symbol)
    # Prefer the poll-supplied live mid; fall back to cached quote.
    if venue_mid is None:
        if not quote or not quote.get("mid"):
            LOG.info(f"[touch_arm] {broker_symbol}/{tf} skip: no venue_mid and no cached quote")
            return
        venue_mid = float(quote["mid"])

    # For gold (XAUUSD+ / XAUTUSDT), venue and analysis are both USD/oz —
    # use venue_mid directly as the live analysis price. No ratio conversion needed.
    live_analysis = venue_mid
    ratio = 1.0  # venue == analysis frame for gold; kept for fulcrum_venue calc below

    LOG.info(f"[touch_arm] {broker_symbol}/{tf} venue_mid={venue_mid:.4f} live_analysis={live_analysis:.4f}")
    trig = touch_arm_trigger(analysis, tf, live_analysis)
    LOG.info(f"[touch_arm] {broker_symbol}/{tf} touch_arm_trigger→{trig.kind if trig else None} edge={trig.fulcrum_price if trig else '-'}")
    if trig is None:
        # Price left the HVN — check if it exited THROUGH the tapped edge (breakout).
        # A breakout through a tapped edge is a valid confirm: sell stops below fire on
        # downward breakout, buy stops above fire on upward breakout.
        _key = (str(account), broker_symbol, tf)
        with ExecBridge._lock:
            _tap = ExecBridge._touch_state.get(_key)
        _breakout_trig = None
        if _tap and _tap.get("trig") is not None:
            _stored = _tap["trig"]
            _tedge = float(_stored.fulcrum_price)
            _tside = str(_stored.context.get("edge", ""))
            # Exited through the tapped edge in the breakout direction
            if _tside == "top" and live_analysis > _tedge:
                _breakout_trig = _stored
            elif _tside == "bottom" and live_analysis < _tedge:
                _breakout_trig = _stored
        # Only clear the tap state if it held a full trigger object (breakout-check path).
        # A plain pending-confirm tap (_tap["trig"] is None) must survive transient
        # price oscillations that return None from touch_arm_trigger — clearing it would
        # reset the confirm-wait on every tick that briefly exits the buffer.
        _has_pending_confirm = _tap is not None and _tap.get("trig") is None
        if not _has_pending_confirm:
            ExecBridge.clear_touch_state(account, broker_symbol, tf)
        if _breakout_trig is None:
            return
        trig = _breakout_trig   # proceed to arm using the stored trigger
        edge = float(trig.fulcrum_price)
        side = str(trig.context.get("edge", ""))
    else:
        edge = float(trig.fulcrum_price)
        side = str(trig.context.get("edge", ""))
        # confirm distance: tick-reversal back inside, in analysis-frame price units
        confirm_ticks = float(grid_cfg.get("touch_arm_confirm_ticks", 0.2) or 0.2)
        if not ExecBridge.touch_arm_check(account, broker_symbol, tf, live_analysis,
                                          edge, side, confirm_ticks):
            # Tap recorded — store the trigger object so breakout-exit can use it
            _key = (str(account), broker_symbol, tf)
            with ExecBridge._lock:
                if _key in ExecBridge._touch_state:
                    ExecBridge._touch_state[_key]["trig"] = trig
            return   # awaiting reversal-confirm or breakout-exit

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
    # Completed-cycle release: we got here flat AND with no active arm → any cycle that armed
    # at this fulcrum is fully closed. Its stale dedup mark would otherwise block re-arm at the
    # same edge forever while price camps there. Drop it so a fresh touch can take orders again.
    ExecBridge.clear_emit(account, broker_symbol, magic=leg_magic)
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
                                        magic=leg_magic,
                                        leg_tp=(not bool(grid_cfg.get("net_profit_exit_only", False))
                                                or bool(grid_cfg.get("leg_tp_ceiling", False))))
    _ratio = (plan.venue_anchor / plan.analysis_anchor) if plan.analysis_anchor else 1.0
    node_low = float(plan.trigger_context.get("node_low", 0.0) or 0.0) * _ratio
    node_high = float(plan.trigger_context.get("node_high", 0.0) or 0.0) * _ratio
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=side,
                            trigger_kind=plan.trigger_kind, venue_mid=venue_mid, magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=len(plan.buy_legs), sell_n=len(plan.sell_legs),
                            buy_lots_total=round(sum(l.lot for l in plan.buy_legs), 4),
                            sell_lots_total=round(sum(l.lot for l in plan.sell_legs), 4),
                            bias_peak=0.0, bias_booked=False, bias_trail_done=False,
                            be_done_buy=False, be_done_sell=False,
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank,
                            sweep_be_usd=float(plan.trigger_context.get("sweep_be_usd") or 0.0),
                            sweep_vwap=float(plan.trigger_context.get("sweep_vwap") or 0.0))
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
    # Detect the EA polling a different account than last seen (persisted across
    # restarts) and retire stale arms left over from the old one — they can never
    # fill once the EA has switched accounts.
    ExecBridge.check_account_switch(account)
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
                                         sell_pnl=float(m.get("sell_pnl", 0.0)),
                                         buy_lots=float(m.get("buy_lots", 0.0)),
                                         sell_lots=float(m.get("sell_lots", 0.0)))
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
    # only 1s-cadence hook). TFs come from triggers list for hvn_inside_touch.
    # Pass the raw venue mid from this poll so touch_arm uses the LIVE intrabar price,
    # not the last bar close (which would miss intrabar edge taps).
    _poll_venue_mid = (float(bid) + float(ask)) / 2.0 if (bid and ask) else None
    if sym and not ExecBridge.daily_target_hit(account):
        _touch_tfs = _trigger_tfs((settings_cfg or {}).get("grid_levels", {}), "hvn_inside_touch")
        for _tf in _touch_tfs:
            try:
                _touch_arm_tf(account, sym, _tf, settings_cfg or {}, venue_mid=_poll_venue_mid)
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
    # surface broker reject codes — an all-fail ack loop is invisible without them
    _fails = [r for r in results if not r.get("ok")]
    if _fails:
        from collections import Counter as _Ctr
        _codes = _Ctr(str(r.get("retcode", "?")) for r in _fails)
        LOG.warning(f"[exec] ack FAILURES account={body.get('account')} "
                    f"retcodes={dict(_codes)} sample={_fails[0]}")
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

    # Triggers-list whitelist gate — only allow single-kind hints that are explicitly
    # listed AND enabled in settings.triggers. "structural" is always exempt (meta-hint).
    # Multi-kind comma hints are also exempt. Any other kind not in the enabled list → skip.
    _gl_cfg = (settings.get("grid_levels") or {})
    _hint_single = trigger_hint.strip() if "," not in trigger_hint else None
    if _hint_single and _hint_single not in ("structural",):
        if _trigger_entry(_gl_cfg, _hint_single) is None:
            return jsonify({"ok": True, "verdict": "skip",
                            "skip_reason": f"{_hint_single}:not_in_triggers"})

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
    # One concurrent cycle per TF for hvn_inside_touch and candle_sweep: an ACTIVE arm
    # occupies the magic even when MT5 momentarily reports flat (legs placed-but-unreported,
    # or pendings mid-reanchor). candle_sweep must not re-arm on every bar close — orders
    # need to survive until filled or the cycle retires (active→False). Re-arm frees only
    # once the cycle retires. Other setups keep the active+flat re-arm fall-through below.
    _once_per_tf_kinds = {"hvn_inside_touch", "candle_sweep"}
    if not force and arm.get("active") and not _live and plan.trigger_kind in _once_per_tf_kinds:
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": "cycle_active(once_per_tf)",
                        "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf})
    # candle_sweep cooldown: even if the cycle was reaped (active→False after SL/manual close),
    # don't immediately re-arm on the next bar. Hold off for N bars so the sweep either:
    #   a) fills and is managed, or b) expires cleanly before a new sweep arm is allowed.
    # cooldown = candle_sweep_cooldown_bars × tf_seconds (default 3 bars).
    if not force and plan.trigger_kind == "candle_sweep" and arm.get("trigger_kind") == "candle_sweep":
        import time as _time
        _tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(tf, 60)
        _cooldown_bars = int((settings.get("grid_levels") or {}).get("candle_sweep_cooldown_bars", 3) or 3)
        _cooldown_secs = _cooldown_bars * _tf_secs
        _last_arm_ts = float(arm.get("ts") or 0.0)
        if _last_arm_ts > 0 and (_time.time() - _last_arm_ts) < _cooldown_secs:
            return jsonify({"ok": True, "verdict": "skip",
                            "skip_reason": f"sweep_cooldown({int(_time.time()-_last_arm_ts)}s/{_cooldown_secs}s)",
                            "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf})
    if not force and arm.get("active") and _live:
        # Bar-close refresh: same tracker as the 1s poll (_refresh_cycle_tps) — frozen
        # guard, drift cap, fade strikes, defer-don't-drop throttle — plus filled-leg TP
        # updates (include_positions=True; positions are refreshed on bar close only).
        # Previously a duplicated inline block with NO vp_frozen guard / drift cap / fade
        # grace, which re-anchored frozen cycles and generated most of the modify churn.
        try:
            _refresh_cycle_tps(account, broker_symbol, symbol, tf, leg_magic, settings,
                               include_positions=True)
            arm = ExecBridge.get_last_arm(account, broker_symbol, magic=leg_magic) or arm
        except Exception:
            pass
        if not arm.get("active"):
            return jsonify({"ok": True, "verdict": "skip", "skip_reason": "hvn_faded",
                            "symbol": symbol, "broker_symbol": broker_symbol, "tf": tf})
        # candle_sweep SL trail — ratcheting, only ever tightens. Engages ONLY after the
        # VWAP-BE has fired (arm.vwap_be_armed) AND the side is >50% filled (matches the
        # hvn_inside_touch deferred-SL threshold). Each bar close in the fill direction moves
        # the stop to that candle's extreme, but only if that beats the current stop:
        #   Buy  positions, GREEN candle (close >= open): SL up to candle LOW  (never down).
        #   Sell positions, RED   candle (close <= open): SL down to candle HIGH (never up).
        # Baseline is the POC the VWAP-BE parked the stop at (sweep_vwap), so the first trail
        # only fires once a favourable candle clears breakeven. Trail level per side is
        # persisted (trail_sl_buy / trail_sl_sell) so the ratchet survives across polls.
        if arm.get("trigger_kind") == "candle_sweep" and arm.get("vwap_be_armed"):
            try:
                _trail_latest = store().latest(symbol, tf)
                if _trail_latest:
                    _ratio_t = float(quote.get("mid") or 0.0) / float(_trail_latest.ohlc.c) \
                               if _trail_latest.ohlc.c else 1.0
                    _trail_lo = round(float(_trail_latest.ohlc.l) * _ratio_t, 4)
                    _trail_hi = round(float(_trail_latest.ohlc.h) * _ratio_t, 4)
                    _is_green = _trail_latest.ohlc.c >= _trail_latest.ohlc.o
                    _is_red   = _trail_latest.ohlc.c <= _trail_latest.ohlc.o
                    _pos_buys  = int(open_state.get("buys",  0) or 0)
                    _pos_sells = int(open_state.get("sells", 0) or 0)
                    _buy_n     = int(arm.get("buy_n")  or 0)
                    _sell_n    = int(arm.get("sell_n") or 0)
                    _be_px     = float(arm.get("sweep_vwap") or 0.0)  # POC = trail baseline
                    _sl_buy    = float(arm.get("trail_sl_buy")  or _be_px)
                    _sl_sell   = float(arm.get("trail_sl_sell") or _be_px)
                    _arm_tp_up   = float(arm.get("tp_up")   or 0.0)
                    _arm_tp_down = float(arm.get("tp_down") or 0.0)
                    _dirty = False
                    # Buy: majority-filled + green + candle low ABOVE current stop → tighten up.
                    if (_pos_buys > 0 and _buy_n > 0 and _pos_buys > _buy_n / 2
                            and _is_green and _trail_lo > 0 and _trail_lo > _sl_buy):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _trail_lo, side="buy",
                                                     comment="FB|sweep_trail|buy",
                                                     tp=_arm_tp_up)
                        arm["trail_sl_buy"] = _trail_lo
                        _dirty = True
                    # Sell: majority-filled + red + candle high BELOW current stop → tighten down.
                    if (_pos_sells > 0 and _sell_n > 0 and _pos_sells > _sell_n / 2
                            and _is_red and _trail_hi > 0
                            and (_sl_sell <= 0 or _trail_hi < _sl_sell)):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _trail_hi, side="sell",
                                                     comment="FB|sweep_trail|sell",
                                                     tp=_arm_tp_down)
                        arm["trail_sl_sell"] = _trail_hi
                        _dirty = True
                    if _dirty:
                        ExecBridge.set_last_arm(account, broker_symbol, **arm)
            except Exception:
                pass

        # hvn_edge SL trail — continuation side only, gated on squeeze_ok (arm-time coil
        # OR runtime expansion upgrade). Trails buy SL to candle low on green bars when
        # breakout_bias=buy; trails sell SL to candle high on red bars when bias=sell.
        if arm.get("trigger_kind") == "hvn_edge":
            try:
                _he_latest = store().latest(symbol, tf)
                if _he_latest:
                    _ratio_he = (float(quote.get("mid") or 0.0) / float(_he_latest.ohlc.c)
                                 if _he_latest.ohlc.c else 1.0)
                    _he_lo = round(float(_he_latest.ohlc.l) * _ratio_he, 4)
                    _he_hi = round(float(_he_latest.ohlc.h) * _ratio_he, 4)
                    _he_green = _he_latest.ohlc.c >= _he_latest.ohlc.o
                    _he_red   = _he_latest.ohlc.c <= _he_latest.ohlc.o
                    _he_bias  = str(arm.get("breakout_bias") or (arm.get("context") or {}).get("breakout_bias") or "")
                    _he_buys  = int(open_state.get("buys",  0) or 0)
                    _he_sells = int(open_state.get("sells", 0) or 0)
                    _he_buy_n  = int(arm.get("buy_n")  or 0)
                    _he_sell_n = int(arm.get("sell_n") or 0)
                    _he_sl_buy  = float(arm.get("trail_sl_buy")  or 0.0)
                    _he_sl_sell = float(arm.get("trail_sl_sell") or 0.0)
                    _he_dirty = False
                    _he_fulcrum = float(arm.get("fulcrum") or 0.0)
                    _he_tp_up   = float(arm.get("tp_up")   or 0.0)
                    _he_tp_down = float(arm.get("tp_down") or 0.0)
                    # Bull break: trail buy (continuation) SL to candle low on green candles.
                    if (_he_bias == "buy" and _he_buys > 0 and _he_buy_n > 0
                            and _he_buys > _he_buy_n / 2
                            and _he_green and _he_lo > 0 and _he_lo > _he_sl_buy):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _he_lo, side="buy",
                                                     comment="FB|hvn_edge_trail|buy",
                                                     tp=_he_tp_up)
                        arm["trail_sl_buy"] = _he_lo
                        _he_dirty = True
                    # Bear break: trail sell (continuation) SL to candle high on red candles.
                    if (_he_bias == "sell" and _he_sells > 0 and _he_sell_n > 0
                            and _he_sells > _he_sell_n / 2
                            and _he_red and _he_hi > 0
                            and (_he_sl_sell <= 0 or _he_hi < _he_sl_sell)):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _he_hi, side="sell",
                                                     comment="FB|hvn_edge_trail|sell",
                                                     tp=_he_tp_down)
                        arm["trail_sl_sell"] = _he_hi
                        _he_dirty = True
                    # Reversion-side deferred SL: once >50% of reversion fills, lock SL at
                    # the fulcrum (the HVN edge the breakout tapped). One-shot per cycle
                    # (be_done_sell / be_done_buy guards repeat fires). Gated by
                    # defer_sl_on_half_fill (False = no committed-side SL; net_target owns exit).
                    _defer_sl_he = bool(_gl_cfg.get("defer_sl_on_half_fill", True))
                    # Bull break → reversion = sells inside HVN → SL at fulcrum (hi edge).
                    if (_defer_sl_he and _he_bias == "buy" and _he_sells > 0 and _he_sell_n > 0
                            and _he_sells > _he_sell_n / 2
                            and not arm.get("be_done_sell") and _he_fulcrum > 0):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _he_fulcrum, side="sell",
                                                     comment="FB|hvn_edge_rev_sl|sell",
                                                     tp=_he_tp_down)
                        arm["be_done_sell"] = True
                        _he_dirty = True
                    # Bear break → reversion = buys inside HVN → SL at fulcrum (lo edge).
                    if (_defer_sl_he and _he_bias == "sell" and _he_buys > 0 and _he_buy_n > 0
                            and _he_buys > _he_buy_n / 2
                            and not arm.get("be_done_buy") and _he_fulcrum > 0):
                        ExecBridge.enqueue_modify_sl(account, broker_symbol, leg_magic,
                                                     _he_fulcrum, side="buy",
                                                     comment="FB|hvn_edge_rev_sl|buy",
                                                     tp=_he_tp_up)
                        arm["be_done_buy"] = True
                        _he_dirty = True
                    if _he_dirty:
                        ExecBridge.set_last_arm(account, broker_symbol, **arm)
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

    # Completed-cycle release: a cycle that has retired (inactive) AND is flat (orders closed)
    # still carries its arm-time dedup mark. Price camping on the same HVN edge keeps the
    # trigger firing at the same fulcrum every bar, so the mark never clears via the no-arm
    # path and re-arm stays stuck on dedup:same_fulcrum forever. Once the orders are closed
    # the next touch is a NEW episode → drop the stale mark so we can take orders again. The
    # active-cycle guards above still prevent stacking (at most one fresh cycle per close).
    if not arm.get("active") and not _live:
        ExecBridge.clear_emit(account, symbol, magic=leg_magic)

    # Touch-only gate: when hvn_inside_touch has touch_only=true in the triggers list,
    # it is armed ONLY by the intrabar touch path (exec_poll → _touch_arm_tf). Bar-close
    # still handles TP refresh for live cycles (handled above). This gate blocks NEW arms
    # from a bar-close detector fire — price may have ended the bar near an edge without
    # a real tap.
    _hit_entry = _trigger_entry((settings.get("grid_levels") or {}), "hvn_inside_touch")
    if (plan.trigger_kind == "hvn_inside_touch"
            and bool((_hit_entry or {}).get("touch_only", False))):
        _emit_audit({"account": account, "symbol": symbol, "tf": tf, "verdict": "skip",
                     "skip_reason": "hvn_inside_touch:touch_only"})
        return jsonify({"ok": True, "verdict": "skip",
                        "skip_reason": "hvn_inside_touch:touch_only",
                        "symbol": symbol, "broker_symbol": broker_symbol})

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
    # leg_tp policy: net_profit_exit_only places legs WITHOUT a per-order TP (net_target
    # owns the exit); leg_tp_ceiling re-adds the structural TP as a FAR ceiling (visible
    # target + runaway books at structure, net_target still primary as it's usually closer).
    # leg_tp = (not net_profit_exit_only) OR leg_tp_ceiling — matches the BTC baseline.
    _gl = (settings.get("grid_levels") or {})
    leg_tp = (not bool(_gl.get("net_profit_exit_only", False))
              or bool(_gl.get("leg_tp_ceiling", False)))
    cmds = ExecBridge.enqueue_grid_plan(account, broker_symbol, plan,
                                        close_first=close_first, clear_kind="cancel",
                                        magic=leg_magic, leg_tp=leg_tp)
    edge = plan.trigger_context.get("edge", "")
    _ratio = (plan.venue_anchor / plan.analysis_anchor) if plan.analysis_anchor else 1.0
    node_low = float(plan.trigger_context.get("node_low", 0.0) or 0.0) * _ratio
    node_high = float(plan.trigger_context.get("node_high", 0.0) or 0.0) * _ratio
    # Count ACTUAL placed commands (some legs may be skipped when already behind market).
    # bias_trail requires buys >= buy_n; using planned len() would make it unreachable
    # for any arm where one or more legs were behind-market at placement time.
    from execution.exec_bridge import PLACE_PENDING as _PP
    _placed_buy  = sum(1 for c in cmds if c.type == _PP and c.order_type == "buy_stop")
    _placed_sell = sum(1 for c in cmds if c.type == _PP and c.order_type == "sell_stop")
    # Planned lot totals per side (sum of each leg's lot, not leg COUNT) — bias_trail
    # gates on lot-weighted fill fraction so a majority-of-LOTS fill (which is what
    # actually commits the cycle's exposure) arms the trail, not just majority-of-legs
    # (near-fulcrum legs are lighter under _ladder's far-side-heavy scaling).
    _buy_lots_total  = round(sum(l.lot for l in plan.buy_legs), 4)
    _sell_lots_total = round(sum(l.lot for l in plan.sell_legs), 4)
    ExecBridge.set_last_arm(account, broker_symbol, tf=tf, fulcrum=plan.fulcrum, edge=edge,
                            trigger_kind=plan.trigger_kind, venue_mid=quote["mid"], magic=leg_magic,
                            n_per_side=plan.n_per_side, step=plan.step, ts=time.time(),
                            buy_n=_placed_buy, sell_n=_placed_sell,
                            buy_lots_total=_buy_lots_total, sell_lots_total=_sell_lots_total,
                            bias_peak=0.0, bias_booked=False, bias_trail_done=False,
                            be_done_buy=False, be_done_sell=False,
                            node_low=round(node_low, 5), node_high=round(node_high, 5),
                            active=True, armed_tf=tf, tp_up=plan.buy_tp, tp_down=plan.sell_tp,
                            max_pos_seen=0, pend_seen=0, flatten_ts=0.0,
                            squeeze_ok=plan.squeeze_ok, squeeze_rank=plan.squeeze_rank,
                            sweep_be_usd=float(plan.trigger_context.get("sweep_be_usd") or 0.0),
                            sweep_vwap=float(plan.trigger_context.get("sweep_vwap") or 0.0),
                            breakout_bias=str(plan.trigger_context.get("breakout_bias") or ""))
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

    _vpc = (settings.get("vp_cache") or {}) if isinstance(settings, dict) else {}
    _draw_dual = bool(_vpc.get("vp_draw_today_zones", False))

    def _collect(period, key):
        return [(float(z["low"]), float(z["high"]))
                for z in ((period.get(key) or []) if period else [])]

    # IST time-band zone drawing — mirrors vp_cache.get(daily) exactly so the chart shows
    # the SAME structure the grid trades on:
    #   < 09:00 IST     → prev-D only (early Asia — today not yet structural)
    #   09:00-19:59 IST → prev-D as hvn/lvn + today as hvn_today/lvn_today (union in play)
    #   >= 20:00 IST    → today primary; prev-D zones only where outside today's range
    #                     (chart analog of the prior-day vacuum fallback)
    # Fallback: if the preferred source is empty, drop to whichever exists.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _ist_hour = _dt.now(tz=_tz(_td(hours=5, minutes=30))).hour

    zones = []
    if _draw_dual:
        # explicit dual-overlay mode: prev-D as hvn/lvn, today as hvn_today/lvn_today
        for lo, hi in _collect(_prev_daily, "hvn_zones"):
            zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_prev_daily, "lvn_zones"):
            zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_today_daily, "hvn_zones"):
            zones.append({"kind": "hvn_today", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_today_daily, "lvn_zones"):
            zones.append({"kind": "lvn_today", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
    elif _ist_hour < 9:
        # pre-09:00: prev-D only (fall back to today if prev-D not yet cached)
        _src = _prev_daily or _today_daily
        for lo, hi in _collect(_src, "hvn_zones"):
            zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_src, "lvn_zones"):
            zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
    elif _ist_hour < 20:
        # 09:00-19:59: both — prev-D as hvn/lvn, today distinct as hvn_today/lvn_today
        _src = _prev_daily or _today_daily
        for lo, hi in _collect(_src, "hvn_zones"):
            zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_src, "lvn_zones"):
            zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        if _today_daily and _today_daily is not _src:
            for lo, hi in _collect(_today_daily, "hvn_zones"):
                zones.append({"kind": "hvn_today", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
            for lo, hi in _collect(_today_daily, "lvn_zones"):
                zones.append({"kind": "lvn_today", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
    else:
        # >= 20:00: today's zones primary; add prev-D zones only outside today's price range
        _today_hvn = _collect(_today_daily, "hvn_zones")
        _today_lvn = _collect(_today_daily, "lvn_zones")
        _today_all = _today_hvn + _today_lvn
        if _today_all:
            _today_floor = min(lo for lo, hi in _today_all)
            _today_ceil  = max(hi for lo, hi in _today_all)
        else:
            _today_floor = _today_ceil = 0.0
        for lo, hi in _today_hvn:
            zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _today_lvn:
            zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        # prev-D zones that don't overlap today's range at all
        _outside = lambda lo, hi: _today_floor == 0.0 or hi <= _today_floor or lo >= _today_ceil
        for lo, hi in _collect(_prev_daily, "hvn_zones"):
            if _outside(lo, hi):
                zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        for lo, hi in _collect(_prev_daily, "lvn_zones"):
            if _outside(lo, hi):
                zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
        # if today has no zones yet, fall back to prev-D entirely
        if not _today_all:
            for lo, hi in _collect(_prev_daily, "hvn_zones"):
                zones.append({"kind": "hvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})
            for lo, hi in _collect(_prev_daily, "lvn_zones"):
                zones.append({"kind": "lvn", "lo": _rebase_price(lo), "hi": _rebase_price(hi)})

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
    # profile. Returns two sessions (prev-D + today) each with start_ts so the EA can
    # anchor each histogram at its session open rather than the visible-range edge.
    _sess_profs = vp_cache.period_profiles_session(symbol)
    # current ROLLING 24h VP appended last — the window the grid trigger's rolling
    # source actually sees (crosses the session boundary, unlike the day profiles).
    _roll = vp_cache.rolling_profile(symbol)
    if _roll:
        _sess_profs = _sess_profs + [_roll]
    profiles = []
    for _sp in _sess_profs:
        _pb = round(float(_sp.get("bin", 0.0)) * _zone_ratio, 5) if _sp.get("bin") else 0.0
        _pp = [{"price": _rebase_price(float(b["price"])), "vol": b["vol"]}
               for b in _sp.get("profile", [])]
        if _pb > 0 and _pp:
            profiles.append({"vp_bin": _pb, "profile": _pp, "start_ts": _sp.get("start_ts", 0)})
    # Backward-compat single profile: use the last entry (most recent session).
    _latest = profiles[-1] if profiles else {}
    profile = _latest.get("profile", [])
    vp_bin = _latest.get("vp_bin", 0.0)
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
    if _trigger_entry(grid_cfg2, "hvn_inside_touch") is not None:
        _buf = float(grid_cfg2.get("hvn_touch_buffer", 0.0) or 0.0) * _zone_ratio
        for z in zones:
            if z["kind"] not in ("hvn", "hvn_today"):
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

    # CVD divergence signals — same pivot-based algorithm as the footprint dashboard
    # (scan_divergences: swing high/low pairs where price makes new extreme but CVD lags).
    cvd_signals = []
    try:
        from pipeline.features.cvd_candlestick import scan_divergences as _cvd_scan
        from pipeline.state_store import store as _cvd_store
        _cvd_tf = zone_tf or "15m"
        _tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                    "1d": 86400}.get(_cvd_tf, 900)
        _cvd_bars = _cvd_store().recent(symbol, _cvd_tf, 80)
        _broker_utc_offset = int(
            ((settings.get("execution") or {}).get("broker_utc_offset_hours") or 0) * 3600
        )
        for _d in _cvd_scan(_cvd_bars, lookback=3):
            cvd_signals.append({
                "bar_time": int(_d["ts"]) - _tf_secs + _broker_utc_offset,
                "price": _rebase_price(float(_d["price"])),
                "direction": "bearish" if _d["type"] == "bear" else "bullish",
            })
    except Exception:
        pass

    # Armed-node overlay: the EXACT node each active cycle triggered on (node_low..node_high
    # + fulcrum), across ALL TFs — already venue-frame (rebased at emit). The drawn HVN/LVN
    # zones are the DAILY VP, but a grid arms on its per-TF ROLLING VP, whose edges can differ
    # (esp. 1m). Drawing the armed node makes the touched edge visible regardless of InpZoneTF.
    armed_nodes = [c for c in _active_cycles
                   if float(c.get("node_low") or 0.0) > 0 and float(c.get("node_high") or 0.0) > 0]

    # Grid cycle overlay: active cycles with TF, strategy, TP lines and profit targets.
    # Parsed by EA to draw TP lines and dashboard rows.
    _gl2 = settings.get("grid_levels") or {}
    _gc_by_tf  = _gl2.get("cycle_net_target_by_tf") or {}
    _gc_act_tf = _gl2.get("bias_trail_activate_by_tf") or {}
    _gc_sq_tm  = float(_gl2.get("squeeze_target_mult", 1.0) or 1.0)
    _gc_sq_tr  = float(_gl2.get("squeeze_trail_mult", 1.0) or 1.0)
    _gc_net_fb = float(_gl2.get("cycle_net_target_usd", 0.0) or 0.0)
    _gc_tr_fb  = float(_gl2.get("bias_trail_activate_usd", 5.0) or 5.0)
    grid_cycles = []
    for _cyc in _active_cycles:
        _mg  = int(_cyc.get("magic") or 0)
        _tf  = tf_from_magic(_mg)
        _net = float(_gc_by_tf.get(_tf, _gc_net_fb) or _gc_net_fb)
        _tr  = float(_gc_act_tf.get(_tf, _gc_tr_fb) or _gc_tr_fb)
        if _cyc.get("squeeze_ok"):
            _net *= _gc_sq_tm
            _tr  *= _gc_sq_tr
        # Trail status for the dashboard: "armed" once bias_peak has cleared the
        # activate threshold (trail is live-tracking, watching for the giveback%),
        # "booked" once it has fired (bias_trail_done, one-shot — see
        # project_arm_magic_key_bug / the double-fire fix for why this is a
        # separate flag from bias_booked), else "off".
        _peak = float(_cyc.get("bias_peak") or 0.0)
        if _cyc.get("bias_trail_done"):
            _trail_status = "booked"
        elif _tr > 0 and _peak >= _tr:
            _trail_status = "armed"
        else:
            _trail_status = "off"
        _bias_side = str(_cyc.get("bias_side") or "")   # persisted by monitor_cycle when peak is set
        grid_cycles.append({
            "magic": _mg, "tf": _tf,
            "kind": _cyc.get("trigger_kind", ""),
            "fulcrum": _cyc.get("fulcrum", 0.0),
            "tp_up": _cyc.get("tp_up", 0.0),
            "tp_down": _cyc.get("tp_down", 0.0),
            "buy_n": _cyc.get("buy_n", 0),
            "sell_n": _cyc.get("sell_n", 0),
            "net_target": round(_net, 2),
            "trail_activate": round(_tr, 2),
            "trail_status": _trail_status,
            "bias_peak": round(_peak, 2),
            "bias_side": _bias_side,
            "squeeze_ok": bool(_cyc.get("squeeze_ok")),
        })

    return jsonify({"ok": True, "zones": zones, "levels": levels, "ict": ict_out,
                    "profile": profile, "vp_bin": vp_bin,
                    "profiles": profiles,
                    "touch_lines": touch_lines,
                    "hvn_cycle_map": hvn_cycle_map,
                    "armed_nodes": armed_nodes,
                    "grid_cycles": grid_cycles,
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


@bp.get("/exec/cycle_status")
def exec_cycle_status():
    """Live snapshot: for every active cycle, show current pnl vs net_target and
    bias_trail thresholds. Useful for verifying exits are evaluating correctly."""
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    account = request.args.get("account", "")
    _settings = current_app.config.get("FB_SETTINGS") or {}
    grid_cfg = _settings.get("grid_levels") or {}
    by_tf = grid_cfg.get("cycle_net_target_by_tf") or {}
    act_by_tf = grid_cfg.get("bias_trail_activate_by_tf") or {}
    giveback = float(grid_cfg.get("bias_trail_giveback_pct", 40.0) or 40.0)
    sq_t_mult = float(grid_cfg.get("squeeze_target_mult", 1.0) or 1.0)
    sq_trail_mult = float(grid_cfg.get("squeeze_trail_mult", 1.0) or 1.0)
    base_fallback = float(grid_cfg.get("cycle_net_target_usd", 0.0) or 0.0)
    trail_fallback = float(grid_cfg.get("bias_trail_activate_usd", 5.0) or 5.0)

    rows = []
    for (acc, sym, mg), arm in ExecBridge._last_arm.items():
        if account and str(acc) != str(account):
            continue
        if not arm.get("active"):
            continue
        tf = tf_from_magic(int(mg or 0))
        net = float(by_tf.get(tf, base_fallback) or base_fallback)
        trail = float(act_by_tf.get(tf, trail_fallback) or trail_fallback)
        if arm.get("squeeze_ok"):
            net   *= sq_t_mult
            trail *= sq_trail_mult
        open_s = ExecBridge.get_open(str(acc), sym, magic=int(mg or 0))
        rows.append({
            "account": str(acc), "symbol": sym, "magic": mg, "tf": tf,
            "trigger_kind": arm.get("trigger_kind", ""),
            "fulcrum": arm.get("fulcrum", 0.0),
            "buy_n": arm.get("buy_n", 0), "sell_n": arm.get("sell_n", 0),
            "buys_open": open_s.get("buys", 0), "sells_open": open_s.get("sells", 0),
            "pendings": open_s.get("pendings", 0),
            "net_target": net, "bias_trail_activate": trail,
            "bias_trail_giveback_pct": giveback,
            "bias_booked": bool(arm.get("bias_booked")),
            "bias_peak": round(float(arm.get("bias_peak") or 0.0), 2),
            "tp_up": arm.get("tp_up", 0.0), "tp_down": arm.get("tp_down", 0.0),
            "squeeze_ok": bool(arm.get("squeeze_ok")),
        })
    return jsonify({"ok": True, "cycles": rows})


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
