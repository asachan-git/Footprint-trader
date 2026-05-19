"""Token budget enforcement.

Rough chars-to-tokens estimate (1 token ~= 4 chars for English) lets us trim
the variable_suffix when it grows past budget. Cached prefix is sized by the
content of the system prompt + few-shot library; we just measure it.
"""

from __future__ import annotations

_CHARS_PER_TOKEN = 4


def est_tokens(s: str) -> int:
    return max(1, len(s) // _CHARS_PER_TOKEN)


def fits(s: str, max_tokens: int) -> bool:
    return est_tokens(s) <= max_tokens


def trim_to(s: str, max_tokens: int) -> str:
    if fits(s, max_tokens):
        return s
    return s[: max_tokens * _CHARS_PER_TOKEN]
