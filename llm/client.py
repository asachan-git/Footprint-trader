"""Anthropic SDK wrapper.

Key design points:
- Splits prompt into cached prefix (system + rules + few-shot) and variable
  suffix (recent bars + features). Sets `cache_control` on the prefix block so
  Anthropic returns the cached tokens at 10% list price after the first call
  within the 5-minute TTL.
- Forces structured output via `tool_choice` on `submit_decision` — no
  free-text parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from anthropic import Anthropic

from .schema import CLAUDE_TOOL, Decision


@dataclass
class ClientConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 600
    timeout_s: float = 8.0


class ClaudeClient:
    def __init__(self, cfg: ClientConfig | None = None) -> None:
        self.cfg = cfg or ClientConfig()
        self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def decide(self, cached_prefix: str, variable_suffix: str) -> Decision:
        resp = self._client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            timeout=self.cfg.timeout_s,
            system=[
                {
                    "type": "text",
                    "text": cached_prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[CLAUDE_TOOL],
            tool_choice={"type": "tool", "name": "submit_decision"},
            messages=[{"role": "user", "content": variable_suffix}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_decision":
                data = dict(block.input)
                data.setdefault("rationale", "")
                return Decision(**data)
        raise RuntimeError("Claude response missing submit_decision tool call")
