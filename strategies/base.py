"""Strategy interface + per-strategy execution context.

A `Strategy` only has to answer one question: given a symbol and the latest bar,
what's the entry Decision (or None for no trade)? Everything else — isolated
stores, the scope that redirects shared execution code at those stores, decision
and equity logging — is handled by `StrategyContext`.
"""

from __future__ import annotations

import abc
import json
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

from llm.schema import Decision
from pipeline.types import Bar

from execution.position_store import PositionStore, use_store
from execution.cycle_store import CycleStore, use_cycle_store

ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = ROOT / "data" / "strategies"
_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


@dataclass
class StrategyContext:
    """Owns one strategy's isolated result files and store handles.

    Shared modules (cycle_manager, PaperExecutor) call the module-level
    position_store()/cycle_store() getters. Within `scope()` those getters are
    redirected to this context's per-strategy stores, so all reads/writes land
    in data/strategies/<name>/ — no edits to the shared modules required.
    """

    name: str
    pstore: PositionStore
    cstore: CycleStore
    dir: Path
    _lock: Lock = field(default_factory=Lock, repr=False)

    @classmethod
    def create(cls, name: str) -> "StrategyContext":
        d = STRATEGIES_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        return cls(
            name=name,
            pstore=PositionStore(log_path=d / "positions.jsonl"),
            cstore=CycleStore(log_path=d / "cycles.jsonl"),
            dir=d,
        )

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def decisions_log(self) -> Path:
        return self.dir / "decisions.jsonl"

    @property
    def equity_log(self) -> Path:
        return self.dir / "equity.jsonl"

    # ── scope ──────────────────────────────────────────────────────────────────
    @contextmanager
    def scope(self):
        """Redirect shared store getters at this strategy's stores for the block."""
        with ExitStack() as stack:
            stack.enter_context(use_store(self.pstore))
            stack.enter_context(use_cycle_store(self.cstore))
            yield self

    # ── logging ────────────────────────────────────────────────────────────────
    def log_decision(self, row: dict) -> None:
        now = int(time.time())
        row = {"ts": now, "ts_ist": _ts_ist(now), "strategy": self.name, **row}
        with self._lock, self.decisions_log.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def log_equity(self, symbol: str, realized_r: float, cum_r: float, reason: str) -> None:
        now = int(time.time())
        row = {
            "ts": now, "ts_ist": _ts_ist(now), "strategy": self.name,
            "symbol": symbol, "realized_r": round(realized_r, 6),
            "cum_r": round(cum_r, 6), "reason": reason,
        }
        with self._lock, self.equity_log.open("a") as fh:
            fh.write(json.dumps(row) + "\n")


@dataclass
class StrategyResult:
    """Per-strategy outcome of one bar tick (for API / logging)."""
    strategy: str
    symbol: str
    action: str            # "flat" | "opened" | "skipped" | "managed" | "error"
    detail: dict = field(default_factory=dict)


class Strategy(abc.ABC):
    """Base class. Subclasses implement `decide`; `name` must be unique."""

    name: str = "base"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    def symbols(self, settings: dict) -> list[str]:
        """Symbols this strategy trades. Defaults to config['symbols'] or the
        global instrument list."""
        syms = self.config.get("symbols")
        if syms:
            return list(syms)
        vp = (settings.get("vp_cache") or {}).get("symbols")
        if vp:
            return list(vp)
        return [settings["instrument"]["symbol"]]

    @abc.abstractmethod
    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        """Return an entry Decision, or None for no trade this bar."""
        raise NotImplementedError

    def adjust_plan(self, plan, bar: Bar, settings: dict):
        """Hook to mutate the built GridPlan before it's filled (default: no-op).

        Lets a strategy override execution policy — e.g. a tighter structural SL —
        without touching the shared grid placer. Return the (possibly modified) plan.
        """
        return plan

    def settings_override(self, settings: dict) -> dict:
        """Per-strategy settings for this tick (manage + enter).

        Returned as a shallow-merged dict so a strategy can change cycle-management
        behavior (e.g. enable a hard-SL exit, disable Claude hedge-eval) without
        mutating the shared settings. Default: unchanged.
        """
        return settings
