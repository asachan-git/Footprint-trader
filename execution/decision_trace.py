"""DecisionTrace — full audit record of one decision pipeline run.

One trace written to data/decisions.jsonl per dispatched decision.
Human-readable summary line also appended to logs/decide_multi.log.

Structure mirrors the 7-stage pipeline:
  1. Regime
  2. Confirmation patterns (absorption / exhaustion / continuation)
  3. Direction (mechanical votes or Claude)
  4. Claude call details (mode, input summary, output)
  5. Grid plan (mode, zones, targets)
  6. Validator result
  7. Dispatch outcome

All fields Optional — stages that don't run stay None.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DECISIONS_JSONL = ROOT / "data" / "decisions.jsonl"
LOG_PATH = ROOT / "logs" / "decide_multi.log"

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class TraceRegime:
    type: str                    # "trend_up" | "trend_down" | "range" | "uncertain"
    confidence: float
    vp_shape: str                # "P" | "b" | "D" | "B" | "unknown"
    shape_regime_agree: bool
    shape_regime_multiplier: float


@dataclass
class TraceCondition:
    name: str
    weight: float
    passed: bool
    detail: str = ""


@dataclass
class TracePattern:
    pattern: str                 # "absorption" | "exhaustion" | "continuation"
    side: str
    weight_total: float
    fired: bool                  # weight_total >= 0.6
    conditions: list[TraceCondition] = field(default_factory=list)

    @property
    def conditions_passed(self) -> list[str]:
        return [c.name for c in self.conditions if c.passed]

    @property
    def conditions_failed(self) -> list[str]:
        return [c.name for c in self.conditions if not c.passed]


@dataclass
class TraceVote:
    module: str
    direction: float
    strength: float
    reason: str


@dataclass
class TraceDirection:
    side: str                    # "long" | "short" | "flat"
    bias_strength: int           # 1–5
    score: float
    votes: list[TraceVote] = field(default_factory=list)
    note: str = ""


@dataclass
class TraceClaude:
    mode: str                    # "full" | "restricted" | "off"
    called: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    input_summary: str = ""      # compact prompt excerpt (not full text)
    output_side: str = ""
    output_confidence: float = 0.0
    output_legs: int = 0
    tp_primary: float | None = None
    sl: float | None = None
    rationale: str = ""


@dataclass
class TraceEntryZone:
    price: float
    source: str                  # "vah" | "val" | "hvn" | "fib_382" | "sweep_wick" | "anchor" | ...
    strength: float
    confluence_score: float
    qty_pct: float


@dataclass
class TraceGrid:
    grid_mode: str               # "mean_reversion" | "trend_continuation" | "cautious"
    entry_zones: list[TraceEntryZone] = field(default_factory=list)
    target_zones: list[float] = field(default_factory=list)
    max_legs: int = 0
    sl: float | None = None


@dataclass
class TraceValidator:
    ok: bool
    score: float
    veto_reason: str | None = None
    details: dict = field(default_factory=dict)


@dataclass
class TraceDispatch:
    status: str                  # "placed" | "skipped" | "rejected"
    position_id: str = ""
    reason: str = ""
    feed_quality_score: float = 1.0


@dataclass
class DecisionTrace:
    decision_id: str
    ts: int                      # unix epoch
    ts_ist: str
    symbol: str
    bar_id: str = ""

    regime: TraceRegime | None = None
    absorption: TracePattern | None = None
    exhaustion: TracePattern | None = None
    continuation: TracePattern | None = None
    direction: TraceDirection | None = None
    claude: TraceClaude | None = None
    grid: TraceGrid | None = None
    validator: TraceValidator | None = None
    dispatch: TraceDispatch | None = None

    counter_trend_exhaustion: bool = False


# ── Serialization ──────────────────────────────────────────────────────────────

def _clean(obj):
    """Recursively convert dataclasses / None-valued dicts for JSON."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _clean(v) for k, v in asdict(obj).items() if v is not None}
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def serialize(trace: DecisionTrace) -> str:
    return json.dumps(_clean(trace), separators=(",", ":"))


def write(trace: DecisionTrace) -> None:
    DECISIONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_JSONL.open("a") as fh:
        fh.write(serialize(trace) + "\n")
    _write_human_log(trace)


def _write_human_log(trace: DecisionTrace) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = [f"[{trace.ts_ist}] {trace.symbol} bar={trace.bar_id}"]
        if trace.regime:
            r = trace.regime
            agree = "Y" if r.shape_regime_agree else "N"
            lines.append(
                f"  regime={r.type}({r.confidence:.2f}) shape={r.vp_shape} "
                f"agree={agree} x{r.shape_regime_multiplier:.2f}"
            )
        patterns = []
        for name, p in [("absorption", trace.absorption),
                        ("exhaustion", trace.exhaustion),
                        ("continuation", trace.continuation)]:
            if p and p.fired:
                patterns.append(f"{name}({p.weight_total:.2f})")
            elif p:
                patterns.append(f"no {name}")
        if patterns:
            lines.append("  patterns: " + ", ".join(patterns))
        if trace.direction:
            d = trace.direction
            lines.append(
                f"  direction: {d.side} bias={d.bias_strength} score={d.score:+.2f}"
                + (f" [counter_trend]" if trace.counter_trend_exhaustion else "")
            )
        if trace.claude and trace.claude.called:
            c = trace.claude
            lines.append(
                f"  claude({c.mode}): side={c.output_side} conf={c.output_confidence:.2f} "
                f"legs={c.output_legs} tp={c.tp_primary} sl={c.sl} "
                f"cache_hit={c.cache_read_tokens > 0}"
            )
        if trace.grid:
            g = trace.grid
            prices = [f"{z.price:.2f}" for z in g.entry_zones[:3]]
            lines.append(
                f"  grid={g.grid_mode} max_legs={g.max_legs} zones=[{', '.join(prices)}] "
                f"tp={g.target_zones[:2]} sl={g.sl}"
            )
        if trace.validator:
            v = trace.validator
            result = "PASS" if v.ok else f"VETO({v.veto_reason})"
            lines.append(f"  validator: {result} score={v.score:.2f}")
        if trace.dispatch:
            d2 = trace.dispatch
            pid = d2.position_id[:8] if d2.position_id else ""
            lines.append(f"  dispatch: {d2.status} pos_id={pid} reason={d2.reason}")

        with LOG_PATH.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        log.debug(f"[DecisionTrace] human log failed: {e}")


# ── Factory helpers ────────────────────────────────────────────────────────────

def new_trace(decision_id: str, symbol: str, bar_id: str = "") -> DecisionTrace:
    import time
    now = int(time.time())
    return DecisionTrace(
        decision_id=decision_id,
        ts=now,
        ts_ist=datetime.fromtimestamp(now, tz=IST).strftime("%Y-%m-%d %H:%M IST"),
        symbol=symbol,
        bar_id=bar_id,
    )
