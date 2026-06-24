"""Direct-MT5 broker adapter — drop-in BrokerAdapter backed by the wine rpyc shim.

Unlike `mt5_adapter.MT5Adapter` (MetaApi.cloud, async, ~200-800ms RTT), this talks
to a LOCAL terminal through `execution.mt5_direct.MT5Direct` (~50ms, no monthly cost)
and — critically — supports SL/TP-modify, partial-close and pending-modify, which the
EA WebRequest bridge cannot do. That is what lets ict_fvg run LIVE (BE moves, partials,
trail) instead of paper.

Routed in via a strategy's settings_override (execution.broker = "vantage_mt5_direct").
The rest of the fleet stays on paper / the existing MetaApi adapter — this adapter only
fires for strategies that opt into it.

Orders are tagged with a DISTINCT magic (execution.direct_magic, default 770010) so the
EA grid (magic 770001) and this never see each other's positions in reconcile/close-all.

Prereq: scripts/mt5_server.sh running + terminal logged in. DEMO ONLY for now.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

from execution.mt5_direct import get_client
from llm.schema import Decision
from pipeline.types import Bar

LOG = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent.parent.parent
_SETTINGS_PATH = _ROOT / "config" / "settings.yaml"


def _load_execution_cfg() -> dict[str, Any]:
    cfg = yaml.safe_load(_SETTINGS_PATH.read_text()) or {}
    return (cfg.get("execution") or {})


_SIDE = {"long": "buy", "short": "sell"}


class MT5DirectAdapter:
    """Sync BrokerAdapter over the localhost wine rpyc MT5 server."""

    def __init__(self) -> None:
        self._cfg = _load_execution_cfg()
        self._symbol_map: dict[str, str] = self._cfg.get("symbol_map", {}) or {}
        self._default_lots: dict[str, float] = self._cfg.get("default_lots", {}) or {}
        self._contract_size: dict[str, float] = self._cfg.get("contract_size", {}) or {}
        self._comment_prefix = str(self._cfg.get("order_comment_prefix", "FB"))
        self._tradable: set[str] = set(self._cfg.get("tradable_symbols") or [])
        self._risk_pct = float(self._cfg.get("risk_per_trade_pct", 0.0) or 0.0)
        self._max_lots = float(self._cfg.get("max_lots", 0.0) or 0.0)
        self._max_spread: dict[str, float] = self._cfg.get("max_spread", {}) or {}
        self._magic = int(self._cfg.get("direct_magic", 770010))
        self._port = int(self._cfg.get("direct_port", 18812))
        self._dev = int(self._cfg.get("direct_deviation", 20))

    @property
    def _c(self):
        return get_client(port=self._port)

    # ── sizing / gates (sync; mirror MT5Adapter's risk math) ───────────────
    def _resolve_lots(self, broker_symbol: str, entry: float | None,
                      stop_loss: float | None) -> tuple[float, dict]:
        default = float(self._default_lots.get(broker_symbol, 0.01))
        sizing: dict[str, Any] = {"method": "default", "default_lot": default}
        if self._risk_pct <= 0 or entry is None or stop_loss is None:
            return default, sizing
        sl_dist = abs(float(entry) - float(stop_loss))
        contract = float(self._contract_size.get(broker_symbol, 0.0))
        if sl_dist <= 0 or contract <= 0:
            return default, sizing
        acct = self._c.account_info()
        balance = float(acct.get("balance") or acct.get("equity") or 0.0)
        if balance <= 0:
            return default, sizing
        info = self._c.symbol_info(broker_symbol)
        step = float(info.get("volume_step") or 0.01)
        vmin = float(info.get("volume_min") or 0.01)
        risk_usd = balance * self._risk_pct
        raw = risk_usd / (sl_dist * contract)
        lots = max(math.floor(raw / step) * step, vmin)
        if self._max_lots > 0:
            lots = min(lots, self._max_lots)
        lots = round(lots, 4)
        sizing = {"method": "risk_adjusted", "balance_usd": balance,
                  "risk_usd": round(risk_usd, 2), "sl_distance": round(sl_dist, 4),
                  "contract_size": contract, "raw_lots": round(raw, 4), "lots": lots}
        return lots, sizing

    def _spread_ok(self, broker_symbol: str) -> tuple[bool, str]:
        cap = float(self._max_spread.get(broker_symbol, 0.0) or 0.0)
        if cap <= 0:
            return True, "no cap"
        t = self._c.tick(broker_symbol)
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask:
            return False, "no quote (market closed?)"
        spread = ask - bid
        return (spread <= cap, f"{spread:.2f}{'≤' if spread <= cap else '>'}{cap}")

    # ── BrokerAdapter: single directional order (market) ───────────────────
    def submit_order(self, decision: Decision, bar: Bar) -> dict:
        if decision.side == "flat":
            return {"broker": "vantage_mt5_direct", "noop": True}
        if self._tradable and bar.symbol not in self._tradable:
            return {"broker": "vantage_mt5_direct", "skipped": f"{bar.symbol} not tradable"}
        if decision.entry is None or decision.stop_loss is None or decision.take_profit is None:
            return {"broker": "vantage_mt5_direct", "error": "missing entry/SL/TP"}
        broker_symbol = self._symbol_map.get(bar.symbol, bar.symbol)
        side = _SIDE.get(decision.side)
        if side is None:
            return {"broker": "vantage_mt5_direct", "error": f"bad side {decision.side}"}
        ok, info = self._spread_ok(broker_symbol)
        if not ok:
            return {"broker": "vantage_mt5_direct", "skipped": f"spread {info}"}
        lots, sizing = self._resolve_lots(broker_symbol, decision.entry, decision.stop_loss)
        qty_pct = float(getattr(decision, "qty_pct", 1.0) or 1.0)
        if 0 < qty_pct < 1.0 and lots > 0:
            lots = round(max(0.01, math.floor((lots * qty_pct) / 0.01) * 0.01), 4)
            sizing["qty_pct"] = qty_pct
        comment = f"{self._comment_prefix}|{decision.side}|{getattr(decision, 'grid_leg', 0)}"
        r = self._c.market_order(broker_symbol, side, lots, sl=decision.stop_loss,
                                 tp=decision.take_profit, magic=self._magic,
                                 comment=comment, deviation=self._dev)
        LOG.info(f"[mt5_direct] {broker_symbol} {side} {lots} SL={decision.stop_loss} "
                 f"TP={decision.take_profit} → {r}")
        return {"broker": "vantage_mt5_direct", "symbol_analysis": bar.symbol,
                "symbol_broker": broker_symbol, "side": decision.side, "lots": lots,
                "stop_loss": decision.stop_loss, "take_profit": decision.take_profit,
                "sizing": sizing, "order": r}

    # ── BrokerAdapter: limit-order grid (ict_fvg = 1-leg via adjust_plan) ──
    def submit_grid(self, plan, bar: Bar) -> dict:
        if self._tradable and bar.symbol not in self._tradable:
            return {"broker": "vantage_mt5_direct", "skipped": f"{bar.symbol} not tradable"}
        ok, info = self._spread_ok(plan.broker_symbol)
        if not ok:
            return {"broker": "vantage_mt5_direct", "skipped": f"spread {info}"}
        side = plan.side  # "long"/"short"
        bside = _SIDE.get(side, side)
        leg_results = []
        for leg in plan.legs:
            comment = f"{self._comment_prefix}|{side}|grid|leg{leg.leg_idx}"
            r = self._c.place_pending(plan.broker_symbol, bside, "limit", leg.price,
                                      leg.lots, sl=plan.safety_sl or 0.0,
                                      tp=plan.take_profit or 0.0, magic=self._magic,
                                      comment=comment, deviation=self._dev)
            leg_results.append({"leg": leg.leg_idx, "price": leg.price,
                                "lots": leg.lots, "result": r})
        return {"broker": "vantage_mt5_direct", "symbol_analysis": bar.symbol,
                "symbol_broker": plan.broker_symbol, "side": side, "grid": True,
                "legs": leg_results, "take_profit": plan.take_profit,
                "safety_sl": plan.safety_sl,
                "avg_entry_on_full_fill": getattr(plan, "avg_entry_on_full_fill", None),
                "tp_source": getattr(plan, "tp_source", ""),
                "bias_strength": getattr(plan, "bias_strength", 3)}

    # ── management (the EA-impossible operations) ──────────────────────────
    def cancel_pending_order(self, order_id: str) -> dict:
        return {"broker": "vantage_mt5_direct", "cancelled": order_id,
                "result": self._c.cancel_pending(int(order_id))}

    def close_position(self, position_id: str) -> dict:
        return {"broker": "vantage_mt5_direct", "closed": position_id,
                "result": self._c.close_position(int(position_id))}

    def partial_close(self, position_id: str, volume: float) -> dict:
        return {"broker": "vantage_mt5_direct", "partial": position_id, "volume": volume,
                "result": self._c.partial_close(int(position_id), float(volume))}

    def modify_position(self, position_id: str, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict:
        if stop_loss is None and take_profit is None:
            return {"broker": "vantage_mt5_direct", "noop": True}
        # MT5 SLTP-modify clears a 0 bound; preserve the side the caller left None.
        if stop_loss is None or take_profit is None:
            cur = next((p for p in self._c.positions(magic=self._magic)
                        if str(p["ticket"]) == str(position_id)), None)
            if cur:
                stop_loss = cur["sl"] if stop_loss is None else stop_loss
                take_profit = cur["tp"] if take_profit is None else take_profit
        return {"broker": "vantage_mt5_direct", "modified": position_id,
                "sl": stop_loss, "tp": take_profit,
                "result": self._c.modify_sltp(int(position_id), stop_loss or 0.0,
                                              take_profit or 0.0)}

    def get_open_positions(self) -> list[dict]:
        return self._c.positions(magic=self._magic)

    def get_quote(self, broker_symbol: str) -> dict:
        t = self._c.tick(broker_symbol)
        s = self._c.symbol_info(broker_symbol)
        bid, ask = t.get("bid"), t.get("ask")
        tick_size = float(s.get("point") or 0.01)
        stops = s.get("stops_level") or 0
        mid = ((bid + ask) / 2) if (bid and ask) else None
        return {"bid": bid, "ask": ask, "mid": mid, "tick_size": tick_size,
                "min_distance": (float(stops) * tick_size if stops else None),
                "ok": bool(bid and ask), "error": None}
