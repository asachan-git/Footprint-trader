"""Vantage MT5 broker adapter via MetaApi.cloud (RPC connection).

Translates a `Decision` from the analysis pipeline (Bybit-symbol space) into
a market order on the Vantage MT5 account (Vantage-symbol space).

Symbol mapping + lot sizing read from settings.execution. Credentials read
from env: METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION.

MetaApi SDK is fully async. We run a persistent asyncio event loop on a
dedicated thread; sync `submit_order` schedules a coroutine and blocks.
Connection is initialised lazily on first order and reused thereafter.

Hedging: Vantage MT5 account is in HEDGING mode, so opposite-direction
orders coexist as separate tickets (see execution/hedge_manager.py).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
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


def _load_execution_cfg() -> dict[str, Any]:
    cfg = yaml.safe_load(_SETTINGS_PATH.read_text()) or {}
    return (cfg.get("execution") or {})


class MT5Adapter:
    """Singleton-ish: one persistent loop+connection per process."""

    _lock = threading.Lock()
    _shared_loop: asyncio.AbstractEventLoop | None = None
    _shared_thread: threading.Thread | None = None
    _shared_conn: Any | None = None
    _spec_cache: dict[str, dict] = {}

    def __init__(self) -> None:
        self._cfg = _load_execution_cfg()
        self._timeout = float(self._cfg.get("metaapi_timeout_s", 30))
        self._symbol_map: dict[str, str] = self._cfg.get("symbol_map", {}) or {}
        self._default_lots: dict[str, float] = self._cfg.get("default_lots", {}) or {}
        self._comment_prefix = str(self._cfg.get("order_comment_prefix", "FB"))
        self._tradable: set[str] = set(self._cfg.get("tradable_symbols") or [])
        self._risk_pct = float(self._cfg.get("risk_per_trade_pct", 0.0) or 0.0)
        self._max_lots = float(self._cfg.get("max_lots", 0.0) or 0.0)

    # ── public sync API ──────────────────────────────────────────────────

    def submit_order(self, decision: Decision, bar: Bar) -> dict:
        if decision.side == "flat":
            return {"broker": "vantage_mt5", "noop": True}
        if self._tradable and bar.symbol not in self._tradable:
            LOG.info(f"[mt5] {bar.symbol} not in tradable_symbols — skipping order")
            return {"broker": "vantage_mt5", "skipped": f"{bar.symbol} not tradable"}
        if decision.entry is None or decision.stop_loss is None or decision.take_profit is None:
            return {"broker": "vantage_mt5", "error": "missing entry/SL/TP"}

        broker_symbol = self._symbol_map.get(bar.symbol, bar.symbol)
        comment = f"{self._comment_prefix}|{decision.side}|leg{decision.grid_leg}"
        try:
            lots, sizing = self._run(self._async_resolve_lots(
                broker_symbol, decision.entry, decision.stop_loss,
            ))
            if lots <= 0:
                return {"broker": "vantage_mt5", "error": f"no lot size resolved for {broker_symbol}", "sizing": sizing}
            result = self._run(self._async_submit(
                broker_symbol=broker_symbol,
                side=decision.side,
                lots=lots,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                comment=comment,
            ))
            LOG.info(
                f"[mt5] order submitted: {broker_symbol} {decision.side} {lots} "
                f"SL={decision.stop_loss} TP={decision.take_profit} → {result}"
            )
            return {
                "broker": "vantage_mt5",
                "symbol_analysis": bar.symbol,
                "symbol_broker": broker_symbol,
                "side": decision.side,
                "lots": lots,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "sizing": sizing,
                "order": result,
            }
        except Exception as e:
            LOG.exception(f"[mt5] order failed: {e}")
            return {"broker": "vantage_mt5", "error": str(e)}

    def close_position(self, position_id: str) -> dict:
        try:
            result = self._run(self._async_close(position_id))
            return {"broker": "vantage_mt5", "closed": position_id, "result": result}
        except Exception as e:
            LOG.exception(f"[mt5] close failed: {e}")
            return {"broker": "vantage_mt5", "error": str(e)}

    def modify_position(
        self,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """Update SL/TP on an existing broker position. Either bound may be None
        to leave that side unchanged."""
        if stop_loss is None and take_profit is None:
            return {"broker": "vantage_mt5", "noop": True}
        try:
            result = self._run(self._async_modify(position_id, stop_loss, take_profit))
            return {"broker": "vantage_mt5", "modified": position_id, "sl": stop_loss, "tp": take_profit, "result": result}
        except Exception as e:
            LOG.exception(f"[mt5] modify failed: {e}")
            return {"broker": "vantage_mt5", "error": str(e)}

    def get_open_positions(self) -> list[dict]:
        try:
            return self._run(self._async_positions())
        except Exception as e:
            LOG.exception(f"[mt5] get_positions failed: {e}")
            return []

    # ── lot sizing ───────────────────────────────────────────────────────

    def _default_lot(self, broker_symbol: str) -> float:
        return float(self._default_lots.get(broker_symbol, 0.0))

    async def _get_spec(self, broker_symbol: str) -> dict:
        cached = MT5Adapter._spec_cache.get(broker_symbol)
        if cached:
            return cached
        conn = await self._ensure_conn()
        spec = await conn.get_symbol_specification(broker_symbol)
        MT5Adapter._spec_cache[broker_symbol] = spec
        return spec

    async def _async_resolve_lots(
        self,
        broker_symbol: str,
        entry: float | None,
        stop_loss: float | None,
    ) -> tuple[float, dict]:
        """Return (lots, sizing_info). Risk-adjusted when possible, else default."""
        sizing: dict[str, Any] = {"method": "default", "default_lot": self._default_lot(broker_symbol)}

        if self._risk_pct <= 0 or entry is None or stop_loss is None:
            return self._default_lot(broker_symbol), sizing

        sl_dist = abs(float(entry) - float(stop_loss))
        if sl_dist <= 0:
            return self._default_lot(broker_symbol), sizing

        conn = await self._ensure_conn()
        info = await conn.get_account_information()
        balance = float(info.get("balance") or info.get("equity") or 0.0)
        if balance <= 0:
            return self._default_lot(broker_symbol), sizing

        spec = await self._get_spec(broker_symbol)
        contract_size = float(spec.get("contractSize") or 0.0)
        if contract_size <= 0:
            return self._default_lot(broker_symbol), sizing
        volume_step = float(spec.get("volumeStep") or 0.01)
        min_vol = float(spec.get("minVolume") or 0.01)

        risk_usd = balance * self._risk_pct
        # USD-denominated symbol shortcut: P&L_per_pt_per_lot ≈ contract_size
        # (true for XAUUSD, BTCUSD, EURUSD-against-USD-balance, etc.)
        raw_lots = risk_usd / (sl_dist * contract_size)
        lots = math.floor(raw_lots / volume_step) * volume_step
        lots = max(lots, min_vol)
        if self._max_lots > 0:
            lots = min(lots, self._max_lots)
        lots = round(lots, 4)

        sizing = {
            "method": "risk_adjusted",
            "balance_usd": balance,
            "risk_pct": self._risk_pct,
            "risk_usd": round(risk_usd, 2),
            "sl_distance": round(sl_dist, 4),
            "contract_size": contract_size,
            "raw_lots": round(raw_lots, 4),
            "lots_after_step": lots,
            "max_lots_cap": self._max_lots,
        }
        return lots, sizing

    # ── persistent async loop ────────────────────────────────────────────

    @classmethod
    def _ensure_loop(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._shared_loop is None or cls._shared_loop.is_closed():
                cls._shared_loop = asyncio.new_event_loop()
                cls._shared_thread = threading.Thread(
                    target=cls._shared_loop.run_forever,
                    name="MT5AdapterLoop",
                    daemon=True,
                )
                cls._shared_thread.start()
            return cls._shared_loop

    def _run(self, coro) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=self._timeout)

    # ── async MetaApi calls ──────────────────────────────────────────────

    async def _ensure_conn(self) -> Any:
        if MT5Adapter._shared_conn is not None:
            return MT5Adapter._shared_conn

        from metaapi_cloud_sdk import MetaApi
        token = os.environ["METAAPI_TOKEN"]
        account_id = os.environ["METAAPI_ACCOUNT_ID"]
        region = os.environ.get("METAAPI_REGION", "new-york")

        api = MetaApi(token, {"region": region})
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
        await account.wait_connected()

        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        MT5Adapter._shared_conn = conn
        LOG.info(f"[mt5] connected to Vantage MT5 (region={region}, account={account_id[:8]}...)")
        return conn

    async def _async_submit(
        self,
        broker_symbol: str,
        side: str,
        lots: float,
        stop_loss: float,
        take_profit: float,
        comment: str,
    ) -> dict:
        conn = await self._ensure_conn()
        if side == "long":
            return await conn.create_market_buy_order(
                broker_symbol, lots,
                stop_loss=stop_loss,
                take_profit=take_profit,
                options={"comment": comment},
            )
        else:
            return await conn.create_market_sell_order(
                broker_symbol, lots,
                stop_loss=stop_loss,
                take_profit=take_profit,
                options={"comment": comment},
            )

    async def _async_close(self, position_id: str) -> dict:
        conn = await self._ensure_conn()
        return await conn.close_position(position_id)

    async def _async_modify(
        self,
        position_id: str,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> dict:
        conn = await self._ensure_conn()
        # MetaApi clears any bound passed as None. Read current values for the
        # bound the caller did NOT specify, so partial-modify preserves the other.
        if stop_loss is None or take_profit is None:
            positions = await conn.get_positions()
            current = next((p for p in (positions or []) if str(p.get("id")) == str(position_id)), None)
            if current:
                if stop_loss is None:
                    stop_loss = current.get("stopLoss")
                if take_profit is None:
                    take_profit = current.get("takeProfit")
        return await conn.modify_position(
            position_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    async def _async_positions(self) -> list[dict]:
        conn = await self._ensure_conn()
        positions = await conn.get_positions()
        return list(positions or [])
