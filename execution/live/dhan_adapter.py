"""Dhan broker adapter — options buying only.

Translates a Decision (populated with option_* fields by /options/decide)
into a market order via dhanhq.

Product routing:
  decision.option_product == "INTRA"  → MIS (squared off by 3:20 PM IST)
  decision.option_product == "MARGIN" → NRML (can hold till expiry)
  fallback                            → INTRA

Lot sizing: reads dhan.default_lots from settings.yaml.
Hard cap:   reads dhan.max_lots from settings.yaml.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from llm.schema import Decision
from pipeline.types import Bar

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
LOG = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_SETTINGS_PATH = _ROOT / "config" / "settings.yaml"


def _load_cfg() -> dict[str, Any]:
    cfg = yaml.safe_load(_SETTINGS_PATH.read_text()) or {}
    return cfg.get("dhan") or {}


class DhanAdapter:
    """Submit options buy orders via dhanhq SDK."""

    def __init__(self) -> None:
        self._cfg = _load_cfg()
        self._max_lots = int(self._cfg.get("max_lots", 1))
        self._default_lots = int(self._cfg.get("default_lots", 1))
        self._sl_pct = float(self._cfg.get("sl_premium_pct", 0.5))
        self._tp_pct = float(self._cfg.get("tp_premium_pct", 1.0))
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            from dhan.auth import get_client
            self._client = get_client()
        return self._client

    def _resolve_lots(self, bar: Bar) -> int:
        try:
            from dhan.instruments import get as get_instr
            per_symbol = (self._cfg.get("lots_per_symbol") or {})
            if bar.symbol in per_symbol:
                return min(int(per_symbol[bar.symbol]), self._max_lots)
        except Exception:
            pass
        return min(self._default_lots, self._max_lots)

    def _resolve_lot_size(self, bar: Bar) -> int:
        try:
            from dhan.instruments import get as get_instr
            return get_instr(bar.symbol).lot_size
        except Exception:
            return 1

    def submit_order(self, decision: Decision, bar: Bar) -> dict:
        if decision.side == "flat":
            return {"broker": "dhan", "noop": True}

        if not decision.option_security_id or decision.option_type == "NONE":
            return {
                "broker": "dhan",
                "error": "Decision missing option_security_id / option_type — "
                         "ensure /options/decide route populated these fields",
            }

        dhan = self._get_client()
        lots = self._resolve_lots(bar)
        lot_size = self._resolve_lot_size(bar)
        quantity = lots * lot_size

        product_type = (
            decision.option_product
            if decision.option_product not in ("NONE", None, "")
            else "INTRA"
        )

        LOG.info(
            f"[dhan] BUY {decision.option_type} {bar.symbol} "
            f"strike={decision.option_strike} expiry={decision.option_expiry} "
            f"qty={quantity} ({lots}L) product={product_type} "
            f"conf={decision.confidence:.2f}"
        )

        try:
            result = dhan.place_order(
                security_id=decision.option_security_id,
                exchange_segment=dhan.NSE_FNO,
                transaction_type=dhan.BUY,
                quantity=quantity,
                order_type=dhan.MARKET,
                product_type=product_type,
                price=0,
                trigger_price=0,
                tag=f"FB_{bar.symbol}_{decision.option_type}",
            )
        except Exception as e:
            LOG.error(f"[dhan] place_order exception: {e}")
            return {"broker": "dhan", "error": str(e)}

        order_id = ""
        if isinstance(result, dict):
            order_id = str(
                result.get("orderId") or result.get("order_id") or
                (result.get("data") or {}).get("orderId") or ""
            )
            status = result.get("status", "")
            if status == "failure" or result.get("remarks"):
                LOG.warning(f"[dhan] order rejected: {result}")
                return {"broker": "dhan", "error": f"order rejected: {result}"}

        fill = {
            "broker": "dhan",
            "order_id": order_id,
            "symbol": bar.symbol,
            "option_type": decision.option_type,
            "strike": decision.option_strike,
            "expiry": decision.option_expiry,
            "quantity": quantity,
            "lots": lots,
            "product": product_type,
            "entry_premium": decision.entry,
            "sl_premium": round(decision.entry * self._sl_pct, 2) if decision.entry else None,
            "tp_premium": round(decision.entry * (1 + self._tp_pct), 2) if decision.entry else None,
            "order_raw": result,
        }

        return fill

    def close_option_position(
        self, security_id: str, quantity: int, product_type: str
    ) -> dict:
        """Place a SELL order to close an open option position."""
        dhan = self._get_client()
        LOG.info(f"[dhan] SELL (close) secid={security_id} qty={quantity} product={product_type}")
        try:
            result = dhan.place_order(
                security_id=security_id,
                exchange_segment=dhan.NSE_FNO,
                transaction_type=dhan.SELL,
                quantity=quantity,
                order_type=dhan.MARKET,
                product_type=product_type,
                price=0,
                trigger_price=0,
                tag="FB_CLOSE",
            )
        except Exception as e:
            LOG.error(f"[dhan] close_option_position exception: {e}")
            return {"error": str(e)}
        return result or {}
