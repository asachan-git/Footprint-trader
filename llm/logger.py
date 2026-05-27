"""Append-only JSONL writer for decisions."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")

from .schema import Decision


ROOT = Path(__file__).resolve().parent.parent


def _path() -> Path:
    p = ROOT / "data" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _md_path() -> Path:
    return ROOT / "data" / "decisions.md"


def log_decision(
    bar_id: str,
    symbol: str,
    tf: str,
    decision: Decision,
    validator_reason: str | None,
    prompt_version: str,
    model: str,
) -> str:
    decision_id = uuid.uuid4().hex
    now = int(time.time())
    rec = {
        "decision_id": decision_id,
        "ts": now,
        "ts_ist": _ts_ist(now),
        "bar_id": bar_id,
        "symbol": symbol,
        "tf": tf,
        "prompt_version": prompt_version,
        "model": model,
        "validator_reason": validator_reason,
        "decision": decision.model_dump(),
    }
    with _path().open("a") as fh:
        fh.write(json.dumps(rec) + "\n")

    _append_md(decision_id, now, symbol, tf, decision, validator_reason, prompt_version)
    return decision_id


def append_dispatch_result(decision_id: str, dispatch_result: dict) -> None:
    """Append a separate dispatch_result record to decisions.jsonl (linked by decision_id)."""
    now = int(time.time())
    rec = {
        "decision_id": decision_id,
        "ts": now,
        "ts_ist": _ts_ist(now),
        "dispatch_result": dispatch_result,
    }
    with _path().open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _append_md(
    decision_id: str,
    ts: int,
    symbol: str,
    tf: str,
    decision: Decision,
    validator_reason: str | None,
    prompt_version: str,
) -> None:
    """Append a human-readable block to data/decisions.md."""
    ts_str = _ts_ist(ts)
    md_path = _md_path()
    md_path.parent.mkdir(parents=True, exist_ok=True)

    if decision.side == "flat":
        line = (
            f"\n`{ts_str}` | **{symbol}** FLAT | conf={decision.confidence:.2f}"
            f" | {decision.rationale[:120] if decision.rationale else '—'}\n"
        )
        with md_path.open("a") as fh:
            fh.write(line)
        return

    # Non-flat: full structured block
    rr = "—"
    if decision.entry and decision.stop_loss and decision.take_profit:
        risk = abs(decision.entry - decision.stop_loss)
        reward = abs(decision.take_profit - decision.entry)
        if risk > 0:
            rr = f"{reward / risk:.1f}"

    rejected = f" ⚠ validator: {validator_reason}" if validator_reason else ""
    side_icon = "📈" if decision.side == "long" else "📉"

    block = f"""
---

## {ts_str} | {symbol} {side_icon} {decision.side.upper()} | conf={decision.confidence:.2f} | R:R={rr} | {prompt_version}{rejected}

**Entry:** {decision.entry} | **SL:** {decision.stop_loss} | **TP:** {decision.take_profit} | **TF:** {tf}
"""

    if decision.entry_reasoning:
        block += f"\n### Entry Reasoning\n{decision.entry_reasoning}\n"
    if decision.sl_reasoning:
        block += f"\n### SL Reasoning\n{decision.sl_reasoning}\n"
    if decision.target_reasoning:
        block += f"\n### Target Reasoning\n{decision.target_reasoning}\n"
    if decision.rationale:
        block += f"\n### Rationale\n{decision.rationale}\n"
    if decision.invalidation_note:
        block += f"\n### Invalidation\n{decision.invalidation_note}\n"

    with md_path.open("a") as fh:
        fh.write(block)
