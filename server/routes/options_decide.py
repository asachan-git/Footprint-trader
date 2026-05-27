"""POST /options/decide — Claude decision for options instruments on Dhan.

Flow:
  1. Load latest option chain snapshot for symbol (from /options/ingest store)
  2. Pull recent OHLCV bars from state_store (same as /decide)
  3. Compute options signals (PCR, OI, IV rank, max pain)
  4. Run strike selector → 3 candidates (ATM, 1OTM, 2OTM)
  5. Build prompt: options_v1 system prompt + bar features + options context
  6. Call Claude → Decision with option_* fields populated
  7. Dispatch to dhan_adapter via router

Body: {
  "symbol": "NIFTY",      # required
  "tf":     "5m",         # optional, defaults to settings primary_tf
  "expiry": "2026-05-29"  # optional, defaults to nearest weekly
}
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from execution.router import dispatch
from execution.position_store import position_store
from llm.client import ClaudeClient, ClientConfig
from llm.logger import log_decision
from llm.validator import validate
from llm.schema import CLAUDE_TOOL
from pipeline.state_store import store
from pipeline.types import Bar
from prompts.builder import variable_suffix

bp = Blueprint("options_decide", __name__)
LOG = logging.getLogger(__name__)

_OPTIONS_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "system" / "options_v1.txt"


def _options_system_prompt() -> str:
    return _OPTIONS_PROMPT_PATH.read_text()


def _build_options_suffix(
    bars: list[Bar],
    options_ctx: dict,
    settings: dict,
) -> str:
    """Build variable suffix combining bar features + options context."""
    # Reuse existing bar feature builder
    base = variable_suffix(bars, settings)
    # Parse and inject options context
    try:
        base_dict = json.loads(base)
    except Exception:
        base_dict = {"raw": base}
    base_dict["options"] = options_ctx
    return json.dumps(base_dict, default=str)


@bp.route("/options/decide", methods=["POST"])
def options_decide():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(force=True, silent=True) or {}

    symbol = str(body.get("symbol") or settings.get("instrument", {}).get("symbol", "NIFTY")).upper()
    tf = str(body.get("tf") or settings.get("instrument", {}).get("primary_tf", "5m"))

    # Load latest option chain snapshot
    from server.routes.options_ingest import get_latest
    snapshot = get_latest(symbol)
    if not snapshot:
        return jsonify({
            "ok": False,
            "skipped": f"No option chain snapshot for {symbol}. "
                       "Run dhan/main.py to start feeding data.",
        })

    chain = snapshot["chain"]
    underlying_ltp = float(snapshot["underlying_ltp"])
    expiry = str(body.get("expiry") or snapshot.get("expiry") or "")

    # Staleness check: reject if snapshot > 5 minutes old during market hours
    age_s = time.time() - snapshot.get("stored_at", 0)
    if age_s > 300:
        return jsonify({
            "ok": False,
            "skipped": f"Option chain snapshot is {age_s:.0f}s old (>5min). "
                       "Data feed may be down.",
        })

    # Pull recent bars from state_store for price action context
    bars = store().recent_bars(symbol, tf, n=settings.get("prompt", {}).get("recent_bars", 10))
    if not bars:
        return jsonify({
            "ok": False,
            "skipped": f"No bars in state_store for {symbol}/{tf}. "
                       "Run dhan/main.py and wait for first bar close.",
        })

    # Compute options signals
    try:
        from options.signal import compute as compute_signal
        signal = compute_signal(chain, underlying_ltp)
    except Exception as e:
        return jsonify({"ok": False, "error": f"options signal failed: {e}"}), 500

    # Select strike candidates
    try:
        from options.strike_selector import select_candidates
        from options.features import build_options_context

        # Pre-filter: get a rough bias from last bar delta for candidate direction hint
        last_bar = bars[-1]
        rough_bias = "long" if last_bar.ohlc.c >= last_bar.ohlc.o else "short"

        candidates = select_candidates(
            chain=chain,
            signal=signal,
            bias=rough_bias,
            confidence=0.7,  # neutral for candidate generation; Claude picks final
            expiry=expiry,
            n_candidates=3,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"strike selection failed: {e}"}), 500

    options_ctx = build_options_context(signal, candidates, underlying_ltp, symbol, expiry)

    # Check for open position (duplicate guard)
    try:
        existing = position_store().open_positions(symbol)
        if existing:
            return jsonify({
                "ok": False,
                "skipped": "position already open",
                "position_id": existing[0].position_id,
            })
    except Exception:
        pass

    # Build prompt
    try:
        suffix = _build_options_suffix(bars, options_ctx, settings)
        system_prompt = _options_system_prompt()
    except Exception as e:
        return jsonify({"ok": False, "error": f"prompt build failed: {e}"}), 500

    # Call Claude
    try:
        claude_cfg = settings.get("claude", {})
        cfg = ClientConfig(
            model=claude_cfg.get("model", "claude-sonnet-4-6"),
            fallback_model=claude_cfg.get("fallback_model", "claude-opus-4-7"),
            max_tokens=int(claude_cfg.get("max_tokens_out", 600)),
            timeout=float(claude_cfg.get("timeout_s", 30)),
            cache=bool(claude_cfg.get("cache", True)),
        )
        client = ClaudeClient(cfg)
        raw_decision = client.decide(
            cached_prefix=system_prompt,
            variable_suffix=suffix,
            tool=CLAUDE_TOOL,
        )
    except Exception as e:
        LOG.error(f"[options_decide] Claude call failed: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": f"Claude call failed: {e}"}), 500

    # Validate
    try:
        decision = validate(raw_decision)
    except Exception as e:
        LOG.warning(f"[options_decide] validation failed: {e}")
        return jsonify({"ok": False, "error": f"validation: {e}"}), 422

    # Dispatch
    try:
        bar = bars[-1]
        result = dispatch(decision, bar, settings)
    except Exception as e:
        LOG.error(f"[options_decide] dispatch failed: {e}")
        return jsonify({"ok": False, "error": f"dispatch: {e}"}), 500

    # Log
    try:
        log_decision(
            symbol=symbol,
            tf=tf,
            bar_id=bars[-1].bar_id,
            decision=decision,
            dispatch_result=result,
            extra={"options": options_ctx},
        )
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "symbol": symbol,
        "side": decision.side,
        "option_type": decision.option_type,
        "strike": decision.option_strike,
        "expiry": decision.option_expiry,
        "confidence": decision.confidence,
        "product": decision.option_product,
        "dispatch": result,
    })
