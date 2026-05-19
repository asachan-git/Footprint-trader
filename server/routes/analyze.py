"""POST /analyze — accept a footprint chart screenshot, send to Claude Vision, return Decision.

Accepts multipart/form-data with field 'image' (PNG/JPG) or JSON with base64 'image_b64'.
"""

from __future__ import annotations

import base64
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from llm.schema import CLAUDE_TOOL, Decision
from llm.validator import validate
from llm.logger import log_decision

bp = Blueprint("analyze", __name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _active_vision_version() -> str:
    p = PROMPTS_DIR / "system" / "current_vision.txt"
    return p.read_text().strip() if p.exists() else "vision_v1"


def _vision_system() -> str:
    version = _active_vision_version()
    return (PROMPTS_DIR / "system" / f"{version}.txt").read_text()


def _call_claude(image_b64: str, media_type: str, settings: dict) -> Decision:
    import os
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = _vision_system()
    resp = client.messages.create(
        model=settings["claude"]["model"],
        max_tokens=settings["claude"]["max_tokens_out"],
        system=system,
        tools=[CLAUDE_TOOL],
        tool_choice={"type": "tool", "name": "submit_decision"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analyze this footprint chart and submit your trading decision.",
                    },
                ],
            }
        ],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_decision":
            data = dict(block.input)
            data.setdefault("rationale", "")
            return Decision(**data)
    raise RuntimeError("Claude response missing submit_decision tool call")


@bp.post("/analyze")
def analyze():
    settings = current_app.config["FB_SETTINGS"]

    # Accept multipart upload or JSON base64
    if request.files.get("image"):
        f = request.files["image"]
        image_bytes = f.read()
        media_type = f.mimetype or "image/png"
        image_b64 = base64.standard_b64encode(image_bytes).decode()
    elif request.is_json:
        body = request.get_json()
        image_b64 = body.get("image_b64", "")
        media_type = body.get("media_type", "image/png")
    else:
        return jsonify({"ok": False, "error": "send image as multipart 'image' field or JSON {image_b64, media_type}"}), 400

    if not image_b64:
        return jsonify({"ok": False, "error": "empty image"}), 400

    try:
        decision = _call_claude(image_b64, media_type, settings)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    validator_reason = validate(decision)
    decision_id = log_decision(
        bar_id="vision",
        symbol=settings["instrument"]["symbol"],
        tf=settings["instrument"]["primary_tf"],
        decision=decision,
        validator_reason=validator_reason,
        prompt_version=_active_vision_version(),
        model=settings["claude"]["model"],
    )

    return jsonify({
        "ok": True,
        "decision_id": decision_id,
        "decision": decision.model_dump(),
        "validator_reason": validator_reason,
    })
