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


SLIPPAGE_PCT = 0.0002   # 0.02% simulated slippage (conservative for liquid markets)


class PaperExecutor:
    def fire(self, decision: Decision, bar: Bar) -> dict:
        if decision.side == "flat":
            return {"mode": "paper", "noop": True}

        # Validate entry/SL/TP exist
        if decision.entry is None or decision.stop_loss is None or decision.take_profit is None:
            LOG.warning(f"[paper] missing entry/SL/TP — skipping {bar.symbol}")
            return {"mode": "paper", "skipped": "missing entry/SL/TP"}

        # Check if there's already an open position for this symbol
        store = position_store()
        open_for_symbol = store.open_positions(bar.symbol)
        if open_for_symbol:
            existing = open_for_symbol[0]
            LOG.info(f"[paper] position already open for {bar.symbol} ({existing.position_id}), skipping")
            return {"mode": "paper", "skipped": "position already open", "position_id": existing.position_id}

        # Simulate slippage: long fills slightly above entry, short slightly below
        slippage = decision.entry * SLIPPAGE_PCT
        if decision.side == "long":
            fill_price = decision.entry + slippage
        else:
            fill_price = decision.entry - slippage

        # Rebuild decision with slipped fill price
        from llm.schema import Decision as _D
        filled_decision = _D(
            side=decision.side,
            entry=round(fill_price, 4),
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            confidence=decision.confidence,
            rationale=decision.rationale,
            grid_leg=decision.grid_leg,
            parent_position_id=decision.parent_position_id,
            add_to_existing=decision.add_to_existing,
            invalidation_note=decision.invalidation_note,
        )

        pos = store.open_position(filled_decision, bar.bar_id, bar.symbol, bar.tf)
        risk = abs(pos.avg_entry - pos.stop_loss)
        rr = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 0
        slip_note = f"(slippage {slippage:+.2f} from signal {decision.entry:.2f})"
        LOG.info(
            f"[paper] OPEN {pos.side} {bar.symbol} fill={pos.avg_entry:.2f} {slip_note} "
            f"sl={pos.stop_loss:.2f} tp={pos.take_profit:.2f} R:R={rr:.1f}"
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
