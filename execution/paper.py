"""Paper executor — logs intent, delegates position lifecycle to position_store.

Paper mode: no real orders. All fills at decision.entry, exits at SL/TP or invalidation.
Position state persisted in data/positions.jsonl (survives restarts).
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from llm.schema import Decision
from pipeline.types import Bar

from .position_store import position_store, position_store_m2, PositionStore

LOG = logging.getLogger(__name__)


SLIPPAGE_PCT = 0.0002   # 0.02% simulated slippage (conservative for liquid markets)


def _sim_lots(symbol: str, entry: float, stop_loss: float, qty_pct: float = 1.0) -> float:
    """Simulate the lot size live would have used — same risk math, paper balance.
    qty_pct (0.3–1.0) scales for confluence-weighted fractional legs."""
    try:
        import yaml
        root = Path(__file__).resolve().parent.parent
        cfg = (yaml.safe_load((root / "config" / "settings.yaml").read_text()) or {}).get("execution", {})
        risk_pct = float(cfg.get("risk_per_trade_pct", 0) or 0)
        balance = float(cfg.get("paper_balance", 0) or 0)
        contract = float((cfg.get("contract_size", {}) or {}).get(symbol, 0) or 0)
        max_lots = float(cfg.get("max_lots", 0) or 0)
        sl_dist = abs(entry - stop_loss)
        if risk_pct <= 0 or balance <= 0 or contract <= 0 or sl_dist <= 0:
            return 0.0
        raw = (balance * risk_pct) / (sl_dist * contract)
        lots = math.floor(raw / 0.01) * 0.01
        if max_lots > 0:
            lots = min(lots, max_lots)
        if 0 < qty_pct < 1.0:
            lots = math.floor((lots * qty_pct) / 0.01) * 0.01
        return round(max(lots, 0.01), 2)
    except Exception:
        return 0.0


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
            # Grid add: honor add_to_existing → append a leg instead of skipping
            if decision.add_to_existing:
                store.add_leg(existing.position_id, decision, bar.bar_id)
                try:
                    from .cycle_store import cycle_store
                    cyc = cycle_store().by_position_id(existing.position_id)
                    if cyc:
                        cycle_store().record_leg(cyc.cycle_id)
                except Exception:
                    pass
                LOG.info(f"[paper] ADD LEG {existing.position_id} leg{decision.grid_leg} @ {decision.entry}")
                return {"mode": "paper", "added_leg": existing.position_id, "leg": decision.grid_leg}
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

        _open_lots = _sim_lots(bar.symbol, filled_decision.entry, filled_decision.stop_loss,
                               qty_pct=float(getattr(decision, "qty_pct", 1.0) or 1.0))
        pos = store.open_position(filled_decision, bar.bar_id, bar.symbol, bar.tf,
                                  lots=_open_lots, source="m1_claude")
        risk = abs(pos.avg_entry - pos.stop_loss)
        rr = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 0
        slip_note = f"(slippage {slippage:+.2f} from signal {decision.entry:.2f})"
        LOG.info(
            f"[paper] OPEN {pos.side} {bar.symbol} fill={pos.avg_entry:.2f} {slip_note} "
            f"sl={pos.stop_loss:.2f} tp={pos.take_profit:.2f} R:R={rr:.1f}"
        )

        # Register cycle for hedge/recovery tracking
        try:
            from execution.cycle_store import cycle_store
            cycle_store().open_cycle(
                symbol=bar.symbol,
                tf=bar.tf,
                direction=pos.side,
                position_id=pos.position_id,
                parent_cycle_id=decision.parent_position_id,
            )
        except Exception:
            pass

        sim_lots = _sim_lots(bar.symbol, pos.avg_entry, pos.stop_loss,
                             qty_pct=float(getattr(decision, "qty_pct", 1.0) or 1.0))

        return {
            "mode": "paper",
            "opened": pos.position_id,
            "side": pos.side,
            "entry": pos.avg_entry,
            "sl": pos.stop_loss,
            "tp": pos.take_profit,
            "rr": round(rr, 2),
            "sim_lots": sim_lots,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
        }

    def submit_grid(self, plan, bar: Bar, _store: PositionStore | None = None) -> dict:
        """Paper simulation of a 5-leg limit grid.

        MVP: fill leg 1 immediately at its limit price; legs 2-5 stored as
        pending in pending_orders.jsonl. cycle_manager will fill them as
        price reaches each leg (checked on every bar close).

        _store: override position store (used by M2 paper A/B to isolate fills).
        """
        from execution.pending_orders import pending_store

        store = _store or position_store()
        open_for_symbol = store.open_positions(bar.symbol)
        if open_for_symbol:
            existing = open_for_symbol[0]
            if existing.side == plan.side:
                LOG.info(f"[paper-grid] same-direction cycle open for {bar.symbol}, skipping")
                return {"mode": "paper", "skipped": "same-direction cycle open",
                        "position_id": existing.position_id}

        # Leg 1 fills immediately as the entry anchor — but NEVER at a better price
        # than the market actually offered this bar. A sell-limit above market
        # (short) or a buy-limit below market (long) is not marketable, so it can't
        # fill at the limit; cap the anchor fill at bar close. Without this cap the
        # anchor booked a phantom favorable entry at the zone price the market never
        # reached after the signal (validated 2026-06-02).
        leg1 = plan.legs[0]
        entry_price = (min(leg1.price, bar.ohlc.c) if plan.side == "short"
                       else max(leg1.price, bar.ohlc.c))
        # Guard: TP must be on profit side of the actual fill. Otherwise anchor opens
        # with TP <= entry (long) or TP >= entry (short) → instant degenerate fill.
        if plan.side == "long" and plan.take_profit <= entry_price:
            LOG.warning(f"[paper-grid] reject {bar.symbol} long: TP {plan.take_profit:.2f} <= entry {entry_price:.2f}")
            return {"mode": "paper", "skipped": "degenerate_tp_long",
                    "tp": plan.take_profit, "entry": entry_price}
        if plan.side == "short" and plan.take_profit >= entry_price:
            LOG.warning(f"[paper-grid] reject {bar.symbol} short: TP {plan.take_profit:.2f} >= entry {entry_price:.2f}")
            return {"mode": "paper", "skipped": "degenerate_tp_short",
                    "tp": plan.take_profit, "entry": entry_price}
        from llm.schema import Decision as _D
        filled = _D(
            side=plan.side,
            entry=round(entry_price, 4),
            stop_loss=plan.safety_sl if plan.safety_sl is not None else (entry_price * 0.95 if plan.side == "long" else entry_price * 1.05),
            take_profit=plan.take_profit,
            # bias/5 — exposure/ordering proxy, NOT a calibrated win-probability.
            confidence=min(1.0, plan.bias_strength / 5.0),
            rationale=f"grid leg1 anchor (bias={plan.bias_strength}/5)",
            grid_leg=1,
            bias_strength=plan.bias_strength,
        )
        src = "m2_rules" if _store is not None else "m1_claude"
        pos = store.open_position(filled, bar.bar_id, bar.symbol, bar.tf,
                                  lots=float(leg1.lots or 0.0), source=src)

        # Legs 2-5 stored as pending
        ps = pending_store()
        pending_ids = []
        for leg in plan.legs[1:]:
            pid = ps.add(
                position_id=pos.position_id,
                symbol=bar.symbol,
                side=plan.side,
                limit_price=leg.price,
                lots=leg.lots,
                leg_idx=leg.leg_idx,
                tp=plan.take_profit,
                safety_sl=plan.safety_sl,
            )
            pending_ids.append(pid)

        try:
            from execution.cycle_store import cycle_store
            cycle_store().open_cycle(
                symbol=bar.symbol, tf=bar.tf,
                direction=plan.side, position_id=pos.position_id,
            )
        except Exception:
            pass

        LOG.info(
            f"[paper-grid] OPEN {plan.side} {bar.symbol} leg1@{entry_price:.2f} "
            f"(zone {leg1.price:.2f}, close {bar.ohlc.c:.2f}) lots={leg1.lots} "
            f"+ {len(plan.legs)-1} pending legs, TP={plan.take_profit:.2f} ({plan.tp_source})"
        )

        return {
            "mode": "paper", "grid": True,
            "position_id": pos.position_id,
            "side": plan.side,
            "leg1_filled": {"price": entry_price, "zone_price": leg1.price, "lots": leg1.lots},
            "pending_legs": [{"id": pid, "price": leg.price, "lots": leg.lots, "leg": leg.leg_idx}
                              for pid, leg in zip(pending_ids, plan.legs[1:])],
            "take_profit": plan.take_profit,
            "tp_source": plan.tp_source,
            "safety_sl": plan.safety_sl,
            "avg_entry_on_full_fill": plan.avg_entry_on_full_fill,
            "bias_strength": plan.bias_strength,
        }
