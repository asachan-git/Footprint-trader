"""Paper executor — logs intent, delegates position lifecycle to position_store.

Paper mode: no real orders. All fills at decision.entry, exits at SL/TP or invalidation.
Position state persisted in data/positions.jsonl (survives restarts).
"""

from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar

from .position_store import position_store

LOG = logging.getLogger(__name__)


class PaperExecutor:
    def fire(self, decision: Decision, bar: Bar) -> dict:
        if decision.side == "flat":
            return {"mode": "paper", "noop": True}

        store = position_store()

        # Check if there's already an open position for this symbol
        open_for_symbol = store.open_positions(bar.symbol)
        if open_for_symbol:
            existing = open_for_symbol[0]
            # Same direction: could add leg (handled by grid_manager later)
            # Opposite direction: don't open (position flip handled by ingest loop)
            LOG.info(f"[paper] position already open for {bar.symbol} ({existing.position_id}), skipping new entry")
            return {"mode": "paper", "skipped": "position already open", "position_id": existing.position_id}

        pos = store.open_position(decision, bar.bar_id, bar.symbol, bar.tf)
        risk = abs(pos.avg_entry - pos.stop_loss)
        rr = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 0
        LOG.info(
            f"[paper] OPEN {pos.side} {bar.symbol} entry={pos.avg_entry:.2f} "
            f"sl={pos.stop_loss:.2f} tp={pos.take_profit:.2f} R:R={rr:.1f} conf={decision.confidence:.2f}"
        )
        return {
            "mode": "paper",
            "opened": pos.position_id,
            "side": pos.side,
            "entry": pos.avg_entry,
            "sl": pos.stop_loss,
            "tp": pos.take_profit,
            "rr": round(rr, 2),
            "confidence": decision.confidence,
            "rationale": decision.rationale,
        }
