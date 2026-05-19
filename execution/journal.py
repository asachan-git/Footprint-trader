"""Journal executor — logs decision intent to logs/journal.jsonl, no fills."""

from __future__ import annotations

import json
import time
from pathlib import Path

from llm.schema import Decision
from pipeline.types import Bar

LOG = Path(__file__).resolve().parent.parent / "logs" / "journal.jsonl"


class JournalExecutor:
    def fire(self, decision: Decision, bar: Bar) -> dict:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        risk = abs((decision.entry or 0) - (decision.stop_loss or 0))
        rr = abs((decision.take_profit or 0) - (decision.entry or 0)) / risk if risk > 0 else 0
        rec = {
            "ts": int(time.time()),
            "bar_id": bar.bar_id,
            "symbol": bar.symbol,
            "tf": bar.tf,
            "side": decision.side,
            "entry": decision.entry,
            "sl": decision.stop_loss,
            "tp": decision.take_profit,
            "risk_pts": round(risk, 4),
            "rr": round(rr, 2),
            "confidence": decision.confidence,
            "rationale": decision.rationale,
        }
        with LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return {"mode": "journal", "logged": True}
