"""POST /decide_multi — analyze multiple symbols simultaneously.

Sends all symbols' footprint context to Claude in one call.
Claude can see cross-instrument correlations (e.g. BTC leading XAUT).
Returns one Decision per symbol.
"""

from __future__ import annotations

import json
import logging
import traceback

from flask import Blueprint, current_app, jsonify, request

from execution.router import dispatch
from llm.client import ClientConfig
from llm.logger import log_decision
from llm.schema import CLAUDE_TOOL, Decision
from llm.validator import validate
from pipeline.state_store import store
from prompts.builder import active_version, cached_prefix, variable_suffix

bp = Blueprint("decide_multi", __name__)
LOG = logging.getLogger(__name__)


def _build_context(symbol: str, tf: str, settings: dict) -> dict | None:
    s = store()
    latest = s.latest(symbol, tf)
    if not latest:
        return None
    recent = s.recent(symbol, tf, settings["prompt"]["recent_bars"])
    higher = {
        htf: s.as_of(symbol, htf, latest.close_ts)
        for htf in settings["instrument"]["timeframes"]
        if htf != tf
    }
    return {
        "symbol": symbol,
        "tf": tf,
        "latest_bar_id": latest.bar_id,
        "context": variable_suffix(recent, higher),
    }


MULTI_TOOL = {
    "name": "submit_multi_decision",
    "description": "Submit one trading decision per symbol analyzed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["long", "short", "flat"]},
                        "entry": {"type": ["number", "null"]},
                        "stop_loss": {"type": ["number", "null"]},
                        "take_profit": {"type": ["number", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                    },
                    "required": ["symbol", "side", "confidence", "rationale"],
                },
            },
            "cross_market_note": {
                "type": "string",
                "description": "Brief note on any cross-instrument correlation observed.",
            },
        },
        "required": ["decisions"],
    },
}


@bp.post("/decide_multi")
def decide_multi():
    settings = current_app.config["FB_SETTINGS"]
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or ["BTCUSDT", "XAUTUSDT"]
    tf = body.get("tf") or settings["instrument"]["primary_tf"]

    contexts = {}
    for sym in symbols:
        ctx = _build_context(sym, tf, settings)
        if ctx:
            contexts[sym] = ctx

    if not contexts:
        return jsonify({"ok": False, "error": "no bars stored for any requested symbol"}), 404

    prefix = cached_prefix(settings["prompt"]["few_shot_count"])
    suffix_parts = [f"=== {sym} ===\n{ctx['context']}" for sym, ctx in contexts.items()]
    combined_suffix = "\n\n".join(suffix_parts)
    combined_suffix += "\n\nAnalyze each instrument above. Note any cross-market correlations."

    import os
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    cfg = ClientConfig(
        model=settings["claude"]["model"],
        max_tokens=settings["claude"]["max_tokens_out"] * len(contexts),
        timeout_s=settings["claude"]["timeout_s"],
    )

    try:
        resp = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout_s,
            system=[{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
            tools=[MULTI_TOOL],
            tool_choice={"type": "tool", "name": "submit_multi_decision"},
            messages=[{"role": "user", "content": combined_suffix}],
        )
    except Exception as e:
        tb = traceback.format_exc()
        LOG.error(f"Claude multi call failed: {e}")
        return jsonify({"ok": False, "error": str(e), "trace": tb.splitlines()[-3:]}), 500

    raw = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_multi_decision":
            raw = block.input
            break

    if not raw:
        return jsonify({"ok": False, "error": "no submit_multi_decision tool call in response"}), 500

    cross_note = raw.get("cross_market_note", "")
    results = []
    version = active_version()

    for d in raw.get("decisions", []):
        sym = d.get("symbol", "")
        ctx = contexts.get(sym)
        if not ctx:
            continue
        d.setdefault("rationale", "")
        decision = Decision(
            side=d["side"],
            entry=d.get("entry"),
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            confidence=float(d.get("confidence", 0)),
            rationale=d["rationale"],
        )
        validator_reason = validate(decision)
        decision_id = log_decision(
            bar_id=ctx["latest_bar_id"],
            symbol=sym,
            tf=tf,
            decision=decision,
            validator_reason=validator_reason,
            prompt_version=version,
            model=cfg.model,
        )
        if validator_reason is None and decision.side != "flat":
            latest = store().latest(sym, tf)
            if latest:
                dispatch(decision, latest, settings)

        results.append({
            "symbol": sym,
            "decision_id": decision_id,
            "decision": decision.model_dump(),
            "validator_reason": validator_reason,
        })

    return jsonify({
        "ok": True,
        "cross_market_note": cross_note,
        "results": results,
    })
