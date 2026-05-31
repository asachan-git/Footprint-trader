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

from .schema import CLAUDE_TOOL, Decision, GRID_PLAN_TOOL, GridDecision


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
        u = resp.usage
        print(
            f"[tokens] in={u.input_tokens} "
            f"cache_created={getattr(u, 'cache_creation_input_tokens', 0)} "
            f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
            f"out={u.output_tokens}"
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_decision":
                data = dict(block.input)
                data.setdefault("rationale", "")
                return Decision(**data)
        raise RuntimeError("Claude response missing submit_decision tool call")

    def decide_grid(self, cached_prefix: str, variable_suffix: str) -> GridDecision:
        """Grid plan path — Claude returns 5-leg plan with price/qty per leg.
        Uses GRID_PLAN_TOOL; same prompt-caching layout as decide().
        """
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
            tools=[GRID_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "submit_grid_plan"},
            messages=[{"role": "user", "content": variable_suffix}],
        )
        u = resp.usage
        print(
            f"[tokens][grid] in={u.input_tokens} "
            f"cache_created={getattr(u, 'cache_creation_input_tokens', 0)} "
            f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
            f"out={u.output_tokens}"
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_grid_plan":
                data = dict(block.input)
                data.setdefault("rationale", "")
                data.setdefault("invalidation_note", "")
                return GridDecision(**data)
        raise RuntimeError("Claude response missing submit_grid_plan tool call")
