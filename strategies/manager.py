"""StrategyManager — in-process fan-out across deployed strategies.

On every bar close the manager loops the enabled strategies and, for each, runs a
fully isolated cycle inside that strategy's `scope()`:

  1. MANAGE — reuse cycle_manager.on_bar_close (disaster floor, VP-anchored TP,
     ChoCh, scale-out). Because the scope redirects the shared store getters, all
     of that logic operates on the strategy's own positions/cycles.
  2. ENTER  — if the strategy has no open position on the symbol, ask it to
     decide(); on a non-flat decision build the mechanical grid and paper-fill it
     into the strategy's stores.
  3. RECORD — append a decision row and, when realized R changes, an equity point.

Common (shared) across strategies: the market data feed, settings, and logs dir.
Per-strategy: everything under data/strategies/<name>/.
"""

from __future__ import annotations

import logging
import traceback

from pipeline.types import Bar

from execution.paper import PaperExecutor
from execution.router import _build_grid_plan, apply_delta_div_tp_cap
from execution import cycle_manager

from .base import StrategyContext, StrategyResult, Strategy
from .registry import build_enabled


def _cvd_div_opposes(symbol: str, side: str, tf: str = "15m", lookback: int = 120) -> bool:
    """True if `side` OPPOSES the most recent confirmed CVD-divergence on `tf`
    (bull div favours long, bear favours short). No div in the window → not opposing
    (gate only removes counter-div entries). Causal: include_live marker from the
    current bars. Used by the opt-in entry gate (config.cvd_div_gate)."""
    try:
        from pipeline.state_store import store
        from pipeline.features.cvd_candlestick import scan_divergences
        bars = store().recent(symbol, tf, lookback)
        if len(bars) < 10:
            return False
        divs = scan_divergences(bars, lookback=3, include_live=True)
        if not divs:
            return False
        last = max(divs, key=lambda d: d["ts"])          # most recent tf CVD-div
        div_is_long = last["type"] == "bull"
        return (side == "long") != div_is_long            # opposes?
    except Exception:
        return False

LOG = logging.getLogger(__name__)


class StrategyManager:
    def __init__(self, strategies: list[Strategy]) -> None:
        self.strategies = strategies
        self._ctx: dict[str, StrategyContext] = {}
        self._last_cum_r: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings: dict | None = None) -> "StrategyManager":
        return cls(build_enabled())

    # ── context cache ──────────────────────────────────────────────────────────
    def ctx(self, name: str) -> StrategyContext:
        c = self._ctx.get(name)
        if c is None:
            c = StrategyContext.create(name)
            self._ctx[name] = c
        return c

    # ── main loop ──────────────────────────────────────────────────────────────
    def tick(self, symbol: str, tf: str, bar: Bar, settings: dict,
             allow_entry: bool = True) -> list[StrategyResult]:
        """Run each strategy for this bar.

        allow_entry=False → manage-only (exits/leg fills) without opening new
        cycles. Call every primary-TF bar with allow_entry=False so exits are
        checked as often as the legacy cycle loop, and once on the decide-TF bar
        with allow_entry=True so entries fire on the intended timeframe.
        """
        results: list[StrategyResult] = []
        for strat in self.strategies:
            if symbol not in strat.symbols(settings):
                continue
            ctx = self.ctx(strat.name)
            try:
                with ctx.scope():
                    results.append(self._run_one(strat, ctx, symbol, tf, bar,
                                                 settings, allow_entry))
            except Exception as e:
                LOG.exception(f"[manager] {strat.name}/{symbol} error: {e}")
                results.append(StrategyResult(
                    strat.name, symbol, "error",
                    {"error": str(e), "trace": traceback.format_exc().splitlines()[-3:]},
                ))
        return results

    def _run_one(self, strat: Strategy, ctx: StrategyContext, symbol: str,
                 tf: str, bar: Bar, settings: dict,
                 allow_entry: bool = True) -> StrategyResult:
        # Per-strategy execution settings (e.g. hard-SL exit, no Claude hedge).
        eff_settings = strat.settings_override(settings)

        # 1. MANAGE existing cycles (shared exit engine, scoped to this strategy)
        try:
            cycle_manager.on_bar_close(symbol, tf, bar, eff_settings)
        except Exception as e:
            LOG.warning(f"[manager] {strat.name}/{symbol} manage failed: {e}")
        self._record_equity(ctx, symbol)

        # 2. ENTER — one cycle per symbol at a time (skipped on manage-only ticks)
        open_pos = ctx.pstore.open_positions(symbol)
        if open_pos:
            return StrategyResult(strat.name, symbol, "skipped",
                                  {"reason": "cycle already open",
                                   "position_id": open_pos[0].position_id})
        # Gate entry to this strategy's own decision TF so a 5m and a 15m instance
        # run in parallel without cross-firing: enter only when the closing `tf`
        # matches the strategy's decide_tf (footprint) / vote_tf (vote panel).
        default_tf = str((settings.get("decide_on_bar_close") or {}).get("tf", "15m"))
        eff_tf = str(strat.config.get("decide_tf") or strat.config.get("vote_tf") or default_tf)
        if not allow_entry or tf != eff_tf:
            return StrategyResult(strat.name, symbol, "managed", {})

        decision = strat.decide(symbol, tf, bar, eff_settings)
        if decision is None or decision.side == "flat":
            return StrategyResult(strat.name, symbol, "flat", {})

        # CVD-div ENTRY gate (opt-in, config.cvd_div_gate) — skip entries whose side
        # OPPOSES the last confirmed 15m CVD-divergence. Validated as a net-positive
        # 15m filter for vote/wave_fib/congress (dryrun_fleet: fleet +52→+80R over 824
        # trades); live A/B vs the ungated twins. Uses 15m explicitly (primary_tf=1m,
        # so the cvd_div_state cache is 1m — not what we want here).
        if strat.config.get("cvd_div_gate") and _cvd_div_opposes(
            symbol, decision.side,
            tf=str(strat.config.get("cvd_div_gate_tf", "15m"))
        ):
            return StrategyResult(strat.name, symbol, "flat",
                                  {"reason": "cvd_div_gate: side opposes last 15m CVD-div"})

        plan = _build_grid_plan(decision, bar, eff_settings)
        plan = strat.adjust_plan(plan, bar, eff_settings)   # strategy execution-policy hook
        # Final TP step: halve TP when entering against a fired 15m delta divergence
        # (opt-in, settings.entry.delta_div_half_tp). Runs LAST so it caps whatever
        # TP the strategy's adjust_plan settled on.
        plan = apply_delta_div_tp_cap(plan, bar, eff_settings)
        # No store args needed: ctx.scope() redirects position_store()/cycle_store()
        # at this strategy's stores, so submit_grid's internal getters land there.
        fill = PaperExecutor().submit_grid(plan, bar)

        pos_id = (fill or {}).get("opened") or (fill or {}).get("position_id")
        ctx.log_decision({
            "symbol": symbol, "side": decision.side,
            "bias_strength": decision.bias_strength,
            "rationale": decision.rationale,
            "position_id": pos_id, "fill": fill,
        })
        action = "opened" if pos_id else "skipped"
        LOG.info(f"[manager] {strat.name}/{symbol} {decision.side} "
                 f"bias={decision.bias_strength} → {action}={pos_id}")
        return StrategyResult(strat.name, symbol, action,
                              {"side": decision.side, "position_id": pos_id, "fill": fill})

    def _record_equity(self, ctx: StrategyContext, symbol: str) -> None:
        """Append an equity point when this strategy's cumulative realized R moves.

        Realized R comes from position_store closes (positions.jsonl) — the only
        source written by the live exit path (cycle_store.close_cycle is not called
        live; cycles.jsonl is offline-only). results.py reads the same source.
        """
        closed = ctx.pstore.closed_positions(n=10_000)
        cum = round(sum(p.realized_r for p in closed), 6)
        prev = self._last_cum_r.get(ctx.name)
        if prev is None:
            self._last_cum_r[ctx.name] = cum
            return
        if cum != prev:
            delta = cum - prev
            newest = max(closed, key=lambda p: p.closed_ts or 0, default=None)
            ctx.log_equity(
                symbol=(newest.symbol if newest else symbol),
                realized_r=delta, cum_r=cum,
                reason=(newest.close_reason if newest else ""),
            )
            self._last_cum_r[ctx.name] = cum

    # ── reporting ──────────────────────────────────────────────────────────────
    def results(self, name: str) -> dict:
        from .results import compute as _compute
        return _compute(self.ctx(name))

    def all_results(self) -> dict:
        return {s.name: self.results(s.name) for s in self.strategies}


# ── process-global singleton ────────────────────────────────────────────────────
_manager: "StrategyManager | None" = None


def get_manager(settings: dict | None = None) -> "StrategyManager":
    """Shared manager instance — same stores + equity tracking across the ingest
    bar-close hook and the /strategies routes."""
    global _manager
    if _manager is None:
        _manager = StrategyManager.from_settings(settings)
    return _manager
