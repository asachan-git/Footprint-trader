"""POST /decide — invoke Claude on the latest bars for a symbol.

Body: {symbol: "BTCUSDT", tf: "1m"}  (both optional; defaults from settings)

Pulls recent bars + MTF context from state_store, builds prompt, calls Claude,
validates, logs decision, dispatches to executor. Idempotent on (symbol, latest bar_id).
"""

from __future__ import annotations

import logging
import traceback

from flask import Blueprint, current_app, jsonify, request

from execution.router import dispatch
from execution.position_store import position_store
from execution.grid_manager import should_add_leg, active_grid_summary
from pipeline.features.session import current_session
from pipeline.features.threshold_calibrator import get as get_thresholds
from llm.client import ClaudeClient, ClientConfig
from llm.logger import log_decision
from llm.validator import validate
from pipeline.footprint import build as build_fp
from pipeline.features import bar_delta, stacked_imbalances, detect_absorption
from pipeline.state_store import store
from pipeline.types import Bar
from typing import Any
from prompts.builder import active_version, cached_prefix, variable_suffix

bp = Blueprint("decide", __name__)
LOG = logging.getLogger(__name__)


def _in_active_hours(filt: dict[str, Any], bar_ts: int) -> tuple[bool, str]:
    """Check active_hours_utc config gate. Empty string = always active."""
    window: str = str(filt.get("active_hours_utc") or "")
    if not window.strip():
        return True, ""
    try:
        start_str, end_str = window.split("-")
        start_h = int(start_str.split(":")[0])
        end_h = int(end_str.split(":")[0])
    except Exception:
        return True, ""
    sess = current_session(bar_ts)
    if start_h <= sess.utc_hour < end_h:
        return True, ""
    return False, f"outside active hours {window} (UTC {sess.utc_hour:02d}:xx, session={sess.session})"


def _same_direction_cooldown(symbol: str, cooldown_bars: int = 3) -> tuple[bool, str]:
    """Block if last N decisions for symbol were same direction (prevents repeated entries).

    Returns (blocked, reason). blocked=True means skip Claude.
    """
    if cooldown_bars <= 0:
        return False, ""
    from pathlib import Path as _Path
    import json as _json
    dlog = _Path("data/decisions.jsonl")
    if not dlog.exists():
        return False, ""
    rows = []
    with dlog.open() as fh:
        for line in fh:
            try:
                r = _json.loads(line)
                if r.get("symbol") == symbol:
                    rows.append(r)
            except Exception:
                pass
    recent = rows[-cooldown_bars:] if len(rows) >= cooldown_bars else rows
    non_flat = [r for r in recent if r.get("decision", {}).get("side") not in ("flat", None)]
    if len(non_flat) < 2:
        return False, ""
    sides = [r["decision"]["side"] for r in non_flat[-2:]]
    if len(set(sides)) == 1:
        return True, f"cooldown: last {len(non_flat)} non-flat decisions all {sides[0]} — wait for reversal or flat"
    return False, ""


def _has_setup(bar: Bar, settings: dict[str, Any]) -> tuple[bool, str]:
    """Pre-filter: only call Claude if local features show a candidate setup."""
    filt: dict[str, Any] = settings.get("decide_filter") or {}
    min_confidence = float(filt.get("min_confidence") or 0)
    require_stacked = bool(filt.get("require_stacked", False))
    require_absorption = bool(filt.get("require_absorption", False))
    cooldown_bars = int(filt.get("same_direction_cooldown", 3))
    primary_tf = str(settings.get("instrument", {}).get("primary_tf", "1m"))

    # Auto-calibrated delta threshold from live bar history
    cal = get_thresholds(bar.symbol, primary_tf, bar.tf)
    config_min = float(filt.get("min_abs_delta") or 0)
    # Use calibrated if sample is sufficient, else fall back to config
    min_abs_delta = cal.min_abs_delta if cal.sample_size >= 8 else config_min
    min_delta_ratio = cal.min_delta_ratio if cal.sample_size >= 8 else 0.0

    # Time filter
    active, time_reason = _in_active_hours(filt, bar.close_ts)
    if not active:
        return False, time_reason

    # Same-direction cooldown
    blocked, cooldown_reason = _same_direction_cooldown(bar.symbol, cooldown_bars)
    if blocked:
        return False, cooldown_reason

    fp = build_fp(bar)
    delta = bar_delta(fp)
    stacked = stacked_imbalances(fp)
    absorptions = detect_absorption(bar, fp)

    if abs(delta) < min_abs_delta:
        return False, f"delta {delta:.2f} below calibrated threshold {min_abs_delta:.1f} (n={cal.sample_size})"
    total_vol = fp.total_bid + fp.total_ask
    if min_delta_ratio > 0 and total_vol > 0 and (abs(delta) / total_vol) < min_delta_ratio:
        return False, f"delta_ratio {abs(delta)/total_vol:.4f} below threshold {min_delta_ratio:.4f}"
    if require_stacked and not stacked:
        return False, "no stacked imbalances"
    if require_absorption and not absorptions:
        return False, "no absorption detected"
    return True, f"filter passed (delta={delta:.1f} thresh={min_abs_delta:.1f} n={cal.sample_size})"


@bp.post("/decide")
def decide():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol") or settings["instrument"]["symbol"]
    primary_tf = body.get("tf") or settings["instrument"]["primary_tf"]
    force = bool(body.get("force", False))

    s = store()
    latest = s.latest(symbol, primary_tf)
    if latest is None:
        return jsonify({"ok": False, "error": f"no bars stored for {symbol} {primary_tf}"}), 404

    if not force:
        passed, reason = _has_setup(latest, settings)
        if not passed:
            return jsonify({
                "ok": True,
                "skipped": True,
                "reason": reason,
                "bar_id": latest.bar_id,
                "note": "no Claude call; pass force:true in body to override",
            })

    recent = s.recent(symbol, primary_tf, settings["prompt"]["recent_bars"])
    higher = {
        tf: s.as_of(symbol, tf, latest.close_ts)
        for tf in settings["instrument"]["timeframes"]
        if tf != primary_tf
    }

    # Check if active grid exists — pass context to Claude
    ps = position_store()
    active_grids = ps.open_positions(symbol)
    active_grid_ctx = active_grid_summary(active_grids[0]) if active_grids else None

    prefix = cached_prefix(settings["prompt"]["few_shot_count"])
    suffix_base = variable_suffix(recent, higher)

    # Inject active grid context into suffix
    import json as _json
    suffix_dict = _json.loads(suffix_base)
    if active_grid_ctx:
        suffix_dict["active_grid"] = active_grid_ctx
        fp_latest = build_fp(latest)
        leg_signal = should_add_leg(latest, fp_latest, active_grids[0])
        suffix_dict["grid_leg_signal"] = {
            "should_add": leg_signal.should_add,
            "reason": leg_signal.reason,
        }
    suffix = _json.dumps(suffix_dict)

    cfg = ClientConfig(
        model=settings["claude"]["model"],
        max_tokens=settings["claude"]["max_tokens_out"],
        timeout_s=settings["claude"]["timeout_s"],
    )

    try:
        client = ClaudeClient(cfg)
        decision = client.decide(prefix, suffix)
    except Exception as e:
        tb = traceback.format_exc()
        LOG.error(f"Claude call failed: {e}\n{tb}")
        print(f"\n[decide ERROR] {e}\n{tb}", flush=True)
        return jsonify({"ok": False, "error": str(e), "trace": tb.splitlines()[-3:]}), 500

    # Confidence threshold gate (post-Claude)
    if min_confidence > 0 and decision.confidence < min_confidence and decision.side != "flat":
        return jsonify({
            "ok": True,
            "skipped": True,
            "reason": f"confidence {decision.confidence:.2f} below threshold {min_confidence}",
            "bar_id": latest.bar_id,
        })

    validator_reason = validate(decision)
    decision_id = log_decision(
        bar_id=latest.bar_id,
        symbol=symbol,
        tf=primary_tf,
        decision=decision,
        validator_reason=validator_reason,
        prompt_version=active_version(),
        model=cfg.model,
    )

    dispatch_result = None
    if validator_reason is None and decision.side != "flat":
        dispatch_result = dispatch(decision, latest, settings)

    return jsonify({
        "ok": True,
        "decision_id": decision_id,
        "bar_id": latest.bar_id,
        "decision": decision.model_dump(),
        "validator_reason": validator_reason,
        "dispatched": dispatch_result,
    })
