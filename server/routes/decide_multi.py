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

# Per-symbol last-fired bar_id cache — prevents re-calling Claude on same bar
_last_bar_ids: dict[str, str] = {}


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


_DECISION_ITEM = {
    "type": "object",
    "properties": {
        "symbol":           {"type": "string"},
        "side":             {"type": "string", "enum": ["long", "short", "flat"]},
        "entry":            {"type": ["number", "null"]},
        "stop_loss":        {"type": ["number", "null"]},
        "take_profit":      {"type": ["number", "null"]},
        "confidence":       {"type": "number", "minimum": 0, "maximum": 1},
        "rationale":        {"type": "string", "description": "Full prose summary"},
        "entry_reasoning":  {"type": "string", "description": "2-3 bullet points confirming entry"},
        "sl_reasoning":     {"type": "string", "description": "1-2 bullets: structural SL level + risk check"},
        "target_reasoning": {"type": "string", "description": "1-2 bullets: specific TP level and why"},
        "grid_leg":         {"type": "integer", "minimum": 1, "maximum": 3},
        "parent_position_id": {"type": ["string", "null"]},
        "add_to_existing":  {"type": "boolean"},
        "invalidation_note": {"type": "string"},
    },
    "required": ["symbol", "side", "confidence", "rationale"],
}

MULTI_TOOL = {
    "name": "submit_multi_decision",
    "description": "Submit one trading decision per symbol analyzed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": _DECISION_ITEM,
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

    force = bool(body.get("force", False))
    contexts = {}
    for sym in symbols:
        ctx = _build_context(sym, tf, settings)
        if not ctx:
            continue
        if not force and _last_bar_ids.get(sym) == ctx["latest_bar_id"]:
            LOG.info(f"[decide_multi] {sym} skipped — same bar_id {ctx['latest_bar_id'][:25]}")
            continue
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
        max_tokens=max(4096, settings["claude"]["max_tokens_out"] * len(contexts) * 3),
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

    if raw is None:
        stop = getattr(resp, "stop_reason", "unknown")
        LOG.error(f"[decide_multi] no tool call in response stop_reason={stop} content_types={[b.type for b in resp.content]}")
        return jsonify({"ok": False, "error": f"no submit_multi_decision tool call in response (stop_reason={stop})"}), 500

    # Mark bar_ids as processed so re-calls on same bar are skipped
    for sym, ctx in contexts.items():
        _last_bar_ids[sym] = ctx["latest_bar_id"]

    cross_note = raw.get("cross_market_note", "")
    if cross_note:
        LOG.info(f"[decide_multi] cross_market_note: {cross_note}")
    results = []
    version = active_version()

    for d in raw.get("decisions", []):
        sym = d.get("symbol", "")
        ctx = contexts.get(sym)
        if not ctx:
            continue
        d.setdefault("rationale", "")
        d.setdefault("entry_reasoning", "")
        d.setdefault("sl_reasoning", "")
        d.setdefault("target_reasoning", "")
        d.setdefault("invalidation_note", "")
        decision = Decision(
            side=d["side"],
            entry=d.get("entry"),
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            confidence=float(d.get("confidence", 0)),
            rationale=d["rationale"],
            entry_reasoning=d["entry_reasoning"],
            sl_reasoning=d["sl_reasoning"],
            target_reasoning=d["target_reasoning"],
            grid_leg=int(d.get("grid_leg", 1)),
            parent_position_id=d.get("parent_position_id"),
            add_to_existing=bool(d.get("add_to_existing", False)),
            bias_strength=int(d.get("bias_strength", 3)),
            invalidation_note=d["invalidation_note"],
        )
        _filt = settings.get("decide_filter") or {}
        rr_floor = float(
            (_filt.get("rr_floor_per_symbol") or {}).get(sym)
            or _filt.get("rr_floor")
            or 1.5
        )
        validator_reason = validate(decision, rr_floor=rr_floor)
        decision_id = log_decision(
            bar_id=ctx["latest_bar_id"],
            symbol=sym,
            tf=tf,
            decision=decision,
            validator_reason=validator_reason,
            prompt_version=version,
            model=cfg.model,
        )
        dispatch_result = None
        if validator_reason is None and decision.side != "flat":
            latest = store().latest(sym, tf)
            if latest:
                dispatch_result = dispatch(decision, latest, settings)
                # Append dispatch result to the jsonl record
                try:
                    from llm.logger import append_dispatch_result
                    append_dispatch_result(decision_id, dispatch_result)
                except Exception as _e:
                    LOG.warning(f"[decide_multi] dispatch result log failed: {_e}")

        results.append({
            "symbol": sym,
            "decision_id": decision_id,
            "decision": decision.model_dump(),
            "validator_reason": validator_reason,
            "dispatched": dispatch_result,
        })

    return jsonify({
        "ok": True,
        "cross_market_note": cross_note,
        "results": results,
    })
