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


def _path() -> Path:
    p = Path(__file__).resolve().parent.parent / "data" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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
    return decision_id
