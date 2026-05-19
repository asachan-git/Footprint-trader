"""Read data/decisions.jsonl, look up forward bars from state_store, call
walk_forward.label(), append to data/outcomes.jsonl.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from pipeline.state_store import store

from .walk_forward import label

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
OUTCOMES = ROOT / "data" / "outcomes.jsonl"


def label_all(max_lookahead: int = 30) -> int:
    if not DECISIONS.exists():
        return 0
    s = store()
    written = 0
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open() as src, OUTCOMES.open("a") as dst:
        for line in src:
            rec = json.loads(line)
            dec = rec["decision"]
            if dec["side"] == "flat" or rec.get("validator_reason"):
                continue
            symbol = rec["symbol"]
            tf = rec["tf"]
            decision_bar = s.as_of(symbol, tf, _close_ts_from_bar_id(rec["bar_id"]))
            if decision_bar is None:
                continue
            all_bars = s.recent(symbol, tf, 10_000)
            try:
                idx = next(i for i, b in enumerate(all_bars) if b.bar_id == decision_bar.bar_id)
            except StopIteration:
                continue
            forward = all_bars[idx + 1 :]
            outcome = label(
                decision_bar_id=rec["bar_id"],
                side=dec["side"],
                entry=dec["entry"],
                sl=dec["stop_loss"],
                tp=dec["take_profit"],
                forward=forward,
                max_lookahead=max_lookahead,
            )
            dst.write(json.dumps({"decision_id": rec["decision_id"], **asdict(outcome)}) + "\n")
            written += 1
    return written


def _close_ts_from_bar_id(bar_id: str) -> int:
    parts = bar_id.split("|")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0
