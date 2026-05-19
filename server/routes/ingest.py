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


def _check_positions(bar, settings) -> list[dict]:
    """Check open positions for SL/TP/invalidation on this bar. Returns list of exit events."""
    ps = position_store()
    exits = []
    max_dd = float(settings.get("risk", {}).get("daily", {}).get("max_dd_r", 99))

    # Daily DD circuit breaker
    if ps.daily_realized_r() < -abs(max_dd):
        for pos in ps.open_positions(bar.symbol):
            ps.invalidate_position(pos.position_id, "daily DD circuit breaker triggered")
            exits.append({"position_id": pos.position_id, "exit": "daily_dd_halt"})
            LOG.warning(f"[ingest] Daily DD halt — closing {pos.position_id}")
        return exits

    fp = build_fp(bar)

    for pos in ps.open_positions(bar.symbol):
        # 1. Hard SL hit
        if pos.side == "long" and bar.ohlc.l <= pos.stop_loss:
            risk = abs(pos.avg_entry - pos.stop_loss)
            realized_r = (pos.stop_loss - pos.avg_entry) / risk if risk > 0 else -1.0
            ps.close_position(pos.position_id, "sl_hit", realized_r)
            exits.append({"position_id": pos.position_id, "exit": "sl_hit", "realized_r": realized_r})
            LOG.info(f"[ingest] SL hit {pos.position_id} long @ {bar.ohlc.l:.2f} ≤ SL {pos.stop_loss:.2f}")

        elif pos.side == "short" and bar.ohlc.h >= pos.stop_loss:
            risk = abs(pos.avg_entry - pos.stop_loss)
            realized_r = (pos.avg_entry - pos.stop_loss) / risk if risk > 0 else -1.0
            ps.close_position(pos.position_id, "sl_hit", realized_r)
            exits.append({"position_id": pos.position_id, "exit": "sl_hit", "realized_r": realized_r})
            LOG.info(f"[ingest] SL hit {pos.position_id} short @ {bar.ohlc.h:.2f} ≥ SL {pos.stop_loss:.2f}")

        # 2. TP absorption exit (full exit when opposite absorption forms at TP)
        elif tp_reason := check_tp_absorption(bar, fp, pos.side, pos.take_profit):
            risk = abs(pos.avg_entry - pos.stop_loss)
            if risk > 0:
                realized_r = abs(pos.take_profit - pos.avg_entry) / risk
            else:
                realized_r = 1.5
            ps.close_position(pos.position_id, f"tp_absorption: {tp_reason}", realized_r)
            exits.append({"position_id": pos.position_id, "exit": "tp_absorption", "realized_r": realized_r, "reason": tp_reason})
            LOG.info(f"[ingest] TP absorption {pos.position_id}: {tp_reason}")

        # 3. Footprint invalidation (opposite absorption at entry zone)
        elif inv := detect_invalidation(bar, fp, pos.side, pos.avg_entry):
            if inv.strength == "strong":
                ps.invalidate_position(pos.position_id, inv.reason)
                exits.append({"position_id": pos.position_id, "exit": "invalidated", "reason": inv.reason})
                LOG.info(f"[ingest] INVALIDATED {pos.position_id}: {inv.reason}")

    return exits


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

        # Snapshot VP + refresh cache if bar crosses day/week boundary
        if prev and bar.tf == primary_tf:
            snapped = snapshot_if_boundary(prev.close_ts, bar.close_ts, bar.symbol, primary_tf)
            if snapped:
                LOG.info(f"[ingest] VP snapshot: {bar.symbol} {snapped}")
                from pipeline.features.vp_cache import build_and_save
                build_and_save([bar.symbol], primary_tf)

    exits = _check_positions(bar, settings)

    # Trail SL / break-even after positions checked (so we don't move SL on a bar that just closed)
    recent_bars = store().recent(bar.symbol, bar.tf, 10)
    sl_adjustments = check_sl_adjustments(bar, recent_bars)

    return jsonify({
        "ok": True,
        "bar_id": bar.bar_id,
        "symbol": bar.symbol,
        "tf": bar.tf,
        "delta": bar.delta,
        "exits": exits,
        "sl_adjustments": [{"position_id": a.position_id, "old": a.old_sl, "new": a.new_sl, "reason": a.reason} for a in sl_adjustments],
    })
