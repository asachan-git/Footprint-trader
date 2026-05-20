"""Position reconciliation: MT5 broker state → internal position_store.

On every bar close (for tradable symbols), this poll:
  1. Asks MT5Adapter for currently-open broker positions (by ticket id)
  2. For each internally-open position with a broker_ticket:
       a. If ticket present in MT5 → still open, no action
       b. If ticket absent → broker closed it (SL/TP hit, manual close,
          stop-out). Fetch deals for that ticket to derive close price,
          compute realized R, write a 'close' event to position_store.
  3. Logs (does NOT auto-close) any broker positions that lack a matching
     internal position — these are external trades / pre-existing tickets.

Safe to call repeatedly. Idempotent — position_store.close_position is
write-once because already-closed positions ignore further close events.
"""

from __future__ import annotations

import logging
from typing import Any

from .position_store import position_store, GridPosition

LOG = logging.getLogger(__name__)


def _realized_r_from_deals(
    deals: list[dict],
    avg_entry: float,
    sl_distance: float,
    side: str,
) -> tuple[float, float]:
    """Return (close_price, realized_r) from MetaApi deals list for a position.

    MetaApi exposes entryType:
      DEAL_ENTRY_IN     → opening fill
      DEAL_ENTRY_OUT    → closing fill (price we want)
      DEAL_ENTRY_INOUT  → reverse (partial close + reopen)
    """
    if not deals:
        return 0.0, 0.0
    exit_deals = [d for d in deals if d.get("entryType") in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT")]
    close_deal = exit_deals[-1] if exit_deals else deals[-1]
    close_price = float(close_deal.get("price") or 0.0)
    if close_price <= 0 or sl_distance <= 0:
        return close_price, 0.0
    if side == "long":
        r = (close_price - avg_entry) / sl_distance
    else:
        r = (avg_entry - close_price) / sl_distance
    return close_price, round(r, 3)


def reconcile(adapter, symbol: str) -> dict:
    """Reconcile one symbol. Returns summary dict.

    `adapter` is the MT5Adapter instance (or any object exposing
    get_open_positions() and an async _ensure_conn + RPC connection).
    """
    store = position_store()
    internal_open = [p for p in store.open_positions(symbol) if p.broker_ticket]
    if not internal_open:
        return {"symbol": symbol, "internal_open": 0, "broker_open": None, "closed": 0}

    try:
        broker_positions = adapter.get_open_positions()
    except Exception as e:
        LOG.warning(f"[reconcile] {symbol}: get_open_positions failed: {e}")
        return {"symbol": symbol, "error": str(e)}

    broker_tickets = {str(p.get("id") or "") for p in (broker_positions or [])}
    closed_count = 0
    closed_details: list[dict] = []

    for pos in internal_open:
        if pos.broker_ticket in broker_tickets:
            continue  # still open broker-side, nothing to do

        # Broker has no record → SL/TP filled or manual close. Pull deals.
        close_price, realized_r = _fetch_close(adapter, pos)
        store.close_position(
            pos.position_id,
            reason=f"broker_closed (ticket {pos.broker_ticket}, close={close_price})",
            realized_r=realized_r,
        )
        # Mirror to cycle_store
        try:
            from .cycle_store import cycle_store
            cs = cycle_store()
            cyc = cs.by_position_id(pos.position_id)
            if cyc:
                cs.close_cycle(cyc.cycle_id, realized_pnl=realized_r, reason="broker_closed")
        except Exception as e:
            LOG.warning(f"[reconcile] cycle close failed for {pos.position_id}: {e}")
        closed_count += 1
        closed_details.append({
            "position_id": pos.position_id,
            "broker_ticket": pos.broker_ticket,
            "side": pos.side,
            "avg_entry": pos.avg_entry,
            "close_price": close_price,
            "realized_r": realized_r,
        })
        LOG.info(
            f"[reconcile] {symbol} closed pos {pos.position_id} (ticket {pos.broker_ticket}) "
            f"side={pos.side} entry={pos.avg_entry} close={close_price} R={realized_r}"
        )

    return {
        "symbol": symbol,
        "internal_open": len(internal_open),
        "broker_open": len(broker_tickets),
        "closed": closed_count,
        "details": closed_details,
    }


def _fetch_close(adapter, pos: GridPosition) -> tuple[float, float]:
    """Fetch the close price + realized R for a closed broker position."""
    sl_distance = abs(pos.avg_entry - pos.stop_loss) if pos.stop_loss else 0.0
    try:
        deals = adapter._run(_async_get_deals(adapter, pos.broker_ticket))
    except Exception as e:
        LOG.warning(f"[reconcile] could not fetch deals for {pos.broker_ticket}: {e}")
        return 0.0, 0.0
    return _realized_r_from_deals(deals or [], pos.avg_entry, sl_distance, pos.side)


async def _async_get_deals(adapter, ticket: str) -> list[dict]:
    """MetaApi: history deals for a closed position. Returns flat list of deal dicts.

    `get_deals_by_position` returns {'deals': [...], 'synchronizing': bool}.
    """
    conn = await adapter._ensure_conn()
    raw = await conn.get_deals_by_position(ticket)
    if isinstance(raw, dict):
        return list(raw.get("deals") or [])
    return list(raw or [])
