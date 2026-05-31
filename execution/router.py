"""Dispatch a Decision to the configured executor.

Mode resolution:
- journal | paper → just instantiate and call
- live → require env ALLOW_LIVE=1; refuse otherwise
"""

from __future__ import annotations

import os
from functools import lru_cache

from llm.schema import Decision
from pipeline.types import Bar

from .base import Executor
from .journal import JournalExecutor
from .paper import PaperExecutor
from .live.broker_base import BrokerAdapter


class LiveTripwireError(RuntimeError):
    pass


class LiveExecutor:
    def __init__(self, broker: BrokerAdapter) -> None:
        self._broker = broker

    def fire(self, decision, bar) -> dict:
        # Duplicate-entry guard: skip a fresh entry when a position is already
        # open for this symbol (unless it's an explicit grid add). Mirrors
        # PaperExecutor — prevents 15m decide cycles from stacking positions.
        if not decision.add_to_existing:
            try:
                from .position_store import position_store
                existing = position_store().open_positions(bar.symbol)
                if existing:
                    return {"mode": "live", "skipped": "position already open",
                            "position_id": existing[0].position_id}
            except Exception:
                pass

        result = self._broker.submit_order(decision, bar)
        # Record live fill into position_store (for reconciliation, journals, etc.)
        broker_ticket = ""
        if isinstance(result, dict):
            order = result.get("order") or {}
            # MT5: positionId nested under "order"; Dhan: order_id at top level
            broker_ticket = str(
                order.get("positionId") or order.get("orderId") or
                result.get("order_id") or ""
            )
        if broker_ticket and not result.get("error") and not result.get("skipped"):
            try:
                from .position_store import position_store
                pos = position_store().open_position(
                    decision=decision, bar_id=bar.bar_id,
                    symbol=bar.symbol, tf=bar.tf,
                    broker_ticket=broker_ticket,
                    fill_type="vantage_mt5_live",
                )
                result["position_id"] = pos.position_id
                # Write option sidecar if this is a Dhan options fill
                if result.get("broker") == "dhan" and result.get("option_type") not in (None, "NONE"):
                    try:
                        from options.position_sidecar import write as _sidecar_write
                        _sidecar_write(pos.position_id, {
                            "security_id": decision.option_security_id or "",
                            "quantity": result.get("quantity", 0),
                            "product_type": result.get("product", "INTRA"),
                            "option_type": result.get("option_type", ""),
                            "strike": result.get("strike"),
                            "expiry": result.get("expiry", ""),
                            "symbol": bar.symbol,
                        })
                    except Exception as _e:
                        import logging as _l
                        _l.getLogger(__name__).warning(f"[live] sidecar write failed: {_e}")
                # Mirror paper: open a cycle for hedge/recovery tracking
                try:
                    from .cycle_store import cycle_store
                    cycle_store().open_cycle(
                        symbol=bar.symbol, tf=bar.tf,
                        direction=pos.side,
                        position_id=pos.position_id,
                        parent_cycle_id=decision.parent_position_id,
                    )
                except Exception as e:
                    import logging as _l
                    _l.getLogger(__name__).warning(f"[live] cycle_store.open_cycle failed: {e}")
                try:
                    from utils.notify import notify
                    notify(
                        "🟢 ORDER FILLED",
                        f"{result.get('symbol_broker', bar.symbol)} {decision.side} "
                        f"{result.get('lots')} lot @ {decision.entry}\n"
                        f"SL {decision.stop_loss}  TP {decision.take_profit}  "
                        f"conf {decision.confidence:.2f}",
                    )
                except Exception:
                    pass
            except Exception as e:
                # Never let store failure mask a successful broker fill
                import logging
                logging.getLogger(__name__).exception(f"[live] position_store.open_position failed: {e}")
                result["position_store_error"] = str(e)
        return {"mode": "live", **result}


@lru_cache(maxsize=1)
def _journal() -> Executor:
    return JournalExecutor()


@lru_cache(maxsize=1)
def _paper() -> Executor:
    return PaperExecutor()


def _live(broker: str = "vantage_mt5") -> Executor:
    if os.environ.get("ALLOW_LIVE") != "1":
        raise LiveTripwireError(
            "mode=live but ALLOW_LIVE env var is not '1' — refusing to fire real orders"
        )
    if broker == "dhan":
        from .live.dhan_adapter import DhanAdapter
        return LiveExecutor(DhanAdapter())
    from .live.mt5_adapter import MT5Adapter
    return LiveExecutor(MT5Adapter())


def _resolve(mode: str, broker: str = "vantage_mt5") -> Executor:
    if mode == "journal":
        return _journal()
    if mode == "paper":
        return _paper()
    if mode == "live":
        return _live(broker)
    raise ValueError(f"unknown mode: {mode!r}")


def _build_grid_plan(decision: Decision, bar: Bar, settings: dict):
    """Construct a GridPlan from a non-flat Decision + current settings."""
    from execution.grid_placer import plan_grid
    from pipeline.features.atr import atr_from_store
    from pipeline.state_store import store as _store

    exec_cfg = settings.get("execution", {}) or {}
    broker_symbol = (exec_cfg.get("symbol_map") or {}).get(bar.symbol, bar.symbol)
    base_lot = float((exec_cfg.get("default_lots") or {}).get(broker_symbol, 0.01))

    atr15 = atr_from_store(bar.symbol, "15m", period=14)
    if atr15 <= 0:
        atr15 = atr_from_store(bar.symbol, "1m", period=14) * 15 or 0.0
    if atr15 <= 0:
        atr15 = max(abs(bar.ohlc.h - bar.ohlc.l), 1e-6) * 5  # last-ditch fallback

    bs = int(getattr(decision, "bias_strength", 3))
    anchor = float(decision.entry) if decision.entry else bar.ohlc.c
    htf_bars = _store().recent(bar.symbol, "15m", 200)

    return plan_grid(
        symbol=bar.symbol, broker_symbol=broker_symbol,
        direction=decision.side, anchor=anchor, bias_strength=bs,
        atr_15m=atr15, base_lot=base_lot, htf_bars=htf_bars,
    )


def dispatch_grid(plan, bar: Bar, settings: dict, parent_position_id: str | None = None) -> dict:
    """Route a GridPlan to the active executor in {paper, live}.

    For LIVE mode, the plan is venue-translated: % offsets from analysis-venue
    anchor are re-applied to the execution-venue (Vantage) live mid quote.
    This decouples Bybit/Binance analysis from Vantage MT5 execution.
    """
    mode = settings.get("mode", "paper")
    broker = str((settings.get("execution") or {}).get("broker") or "vantage_mt5")

    # Venue translation (live mode only — paper uses Bybit price directly)
    if mode == "live" and getattr(plan, "leg_offsets_pct", ()):
        try:
            from execution.grid_placer import to_normalized
            from execution.venue_translator import translate, fetch_venue_quote
            norm = to_normalized(plan)
            quote = fetch_venue_quote(broker, plan.broker_symbol)
            translated = translate(norm, quote, plan.broker_symbol, plan.side)
            import logging as _l
            _l.getLogger(__name__).info(
                f"[router] venue-translated {plan.symbol}→{plan.broker_symbol}: "
                f"bybit_anchor={plan.anchor_price:.2f} → vantage_anchor={translated.anchor_price:.2f} "
                f"(quote_ok={quote.get('ok')}, tick={quote.get('tick_size')})"
            )
            plan = translated
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning(f"[router] venue translation failed (using raw plan): {e}")

    if mode == "live":
        if os.environ.get("ALLOW_LIVE") != "1":
            raise LiveTripwireError("mode=live but ALLOW_LIVE != '1'")
        adapter = _live(broker)._broker  # type: ignore[attr-defined]
        result = adapter.submit_grid(plan, bar)
        result["parent_position_id"] = parent_position_id
        return {"mode": "live", **result}
    elif mode == "paper":
        if settings.get("_m2_paper"):
            from execution.position_store import position_store_m2 as _ps_m2
            result = _paper().submit_grid(plan, bar, _store=_ps_m2())  # type: ignore[attr-defined]
        else:
            result = _paper().submit_grid(plan, bar)  # type: ignore[attr-defined]
        result["parent_position_id"] = parent_position_id
        return result
    else:
        return {"mode": mode, "skipped": "grid not supported in journal mode"}


def dispatch(decision: Decision, bar: Bar, settings: dict) -> dict:
    # Tier 1.1 — hard equity kill switch.
    # Halts ALL new entries if today's realized R drops below daily_loss_floor.
    # Manual reset: restart server or clear today's closed positions.
    # force=True in /decide body bypasses (for testing only).
    try:
        _floor = -abs(float(((settings.get("risk") or {}).get("daily") or {}).get("max_dd_r", 5.0)))
        from .position_store import position_store as _ps
        _daily_r = _ps().daily_realized_r()
        if _daily_r <= _floor and decision.side != "flat":
            import logging as _l
            _l.getLogger(__name__).warning(
                f"[router][kill_switch] HALT — daily_R={_daily_r:.2f} ≤ floor={_floor:.1f}. "
                f"No new entries until manual reset (server restart)."
            )
            return {
                "mode": settings.get("mode", "paper"),
                "skipped": f"kill_switch:daily_R={_daily_r:.2f}≤{_floor:.1f}",
                "daily_r": _daily_r,
                "floor": _floor,
            }
    except Exception as _ke:
        import logging as _l
        _l.getLogger(__name__).debug(f"[router] kill_switch check failed: {_ke}")

    trading_mode = str(settings.get("trading_mode") or "buy_sell_only")

    # Grid path: trading_mode in {buy_sell_only, grid} → fire mechanical 5-leg grid
    if trading_mode in ("buy_sell_only", "grid") and decision.side != "flat":
        # Regime gate — refuse trades against confirmed trends
        try:
            from pipeline.features.day_type import get_regime, is_blocked_by_regime
            primary_tf = str(settings.get("instrument", {}).get("primary_tf", "1m"))
            regime = get_regime(bar.symbol, primary_tf)
            floor = float((settings.get("regime") or {}).get("block_against_trend_confidence", 0.75))
            blocked, reason = is_blocked_by_regime(regime, decision.side, confidence_floor=floor)
            if blocked:
                import logging as _l
                _l.getLogger(__name__).info(f"[router] {bar.symbol} {reason}")
                return {"mode": settings["mode"], "skipped": reason,
                        "regime": regime.type, "regime_confidence": regime.confidence}
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning(f"[router] regime gate skipped: {e}")

        # Same-direction cycle guard
        try:
            from .position_store import position_store as _ps
            same_dir = [p for p in _ps().open_positions(bar.symbol) if p.side == decision.side]
            if same_dir and not decision.add_to_existing:
                return {"mode": settings["mode"], "skipped": f"same-direction grid already open",
                        "position_id": same_dir[0].position_id}
        except Exception:
            pass
        # Decision veto layer — auction + confluence + session + threshold checks
        try:
            from execution.decision_validator import validate as _dv_validate
            _vr = _dv_validate(decision.side, bar.symbol, bar, settings,
                               entry_price=decision.entry)
            if not _vr.ok:
                import logging as _l
                _l.getLogger(__name__).info(
                    f"[router][veto] {bar.symbol} {decision.side} rejected: {_vr.veto_reason} "
                    f"score={_vr.score:.2f}"
                )
                return {"mode": settings["mode"], "skipped": f"veto:{_vr.veto_reason}",
                        "veto_score": _vr.score, "veto_details": _vr.details}
        except Exception as _ve:
            import logging as _l
            _l.getLogger(__name__).debug(f"[router] decision_validator skipped: {_ve}")

        try:
            plan = _build_grid_plan(decision, bar, settings)
            return dispatch_grid(plan, bar, settings)
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).exception(f"[router] grid dispatch failed: {e}")
            return {"mode": settings["mode"], "error": f"grid_build_failed: {e}"}

    # Legacy single-order path (only if trading_mode explicitly disables grid)
    if decision.side != "flat" and not decision.add_to_existing:
        try:
            from .position_store import position_store as _ps
            same_dir = [p for p in _ps().open_positions(bar.symbol) if p.side == decision.side]
            if same_dir:
                import logging as _l
                _l.getLogger(__name__).info(
                    f"[router] {bar.symbol} {decision.side} skipped — "
                    f"same-direction position already open ({same_dir[0].position_id})"
                )
                return {"mode": settings["mode"], "skipped": f"same-direction {decision.side} already open",
                        "position_id": same_dir[0].position_id}
        except Exception:
            pass
    broker = str((settings.get("execution") or {}).get("broker") or "vantage_mt5")
    return _resolve(settings["mode"], broker).fire(decision, bar)
