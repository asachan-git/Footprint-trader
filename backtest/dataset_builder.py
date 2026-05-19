"""Join decisions + outcomes → training dataset JSONL.

Each row: {state_summary, decision, outcome}. State summary is the prompt's
variable_suffix that Claude saw at the time, so a future fine-tune sees the
same inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
OUTCOMES = ROOT / "data" / "outcomes.jsonl"


def build(out_path: Path) -> int:
    if not DECISIONS.exists() or not OUTCOMES.exists():
        return 0
    outcomes_by_id: dict[str, dict] = {}
    with OUTCOMES.open() as fh:
        for line in fh:
            rec = json.loads(line)
            outcomes_by_id[rec["decision_id"]] = rec

    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open() as src, out_path.open("w") as dst:
        for line in src:
            d = json.loads(line)
            o = outcomes_by_id.get(d["decision_id"])
            if not o:
                continue
            dst.write(json.dumps({
                "bar_id": d["bar_id"],
                "symbol": d["symbol"],
                "tf": d["tf"],
                "prompt_version": d["prompt_version"],
                "model": d["model"],
                "decision": d["decision"],
                "outcome": {k: v for k, v in o.items() if k != "decision_id"},
            }) + "\n")
            written += 1
    return written
