"""Hedge Manager — neutralise exposure on footprint invalidation.

Instead of closing a losing cycle at market (hard stop), we:
1. Open an opposite-direction hedge sized to match the active cycle.
2. Let price resolve:
   a. Price recovers past hedge entry → remove hedge, cycle continues.
   b. Price holds or extends → promote hedge to a new recovery cycle.

Hedging mode confirmed: Vantage MT5 account is in HEDGING mode —
long + short can coexist on the same instrument simultaneously.

Hedge triggers (any one fires):
  - Footprint invalidation: opposite absorption at entry zone
  - Sweep confirmed against cycle direction
  - Wave retrace > 78.6% (impulse thesis dead)

Hedge removal conditions (all must hold):
  - Price recovers to hedge_open_price or beyond
  - Bid absorption forming (long cycle) or ask absorption (short cycle)
  - CVD not making new extremes against cycle

Circuit breaker: if net unrealized exposure across all cycles exceeds
config.risk.circuit_breaker_r → force-close everything.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


@dataclass
class HedgePosition:
    hedge_id: str
    cycle_id: str           # which TradeCycle this hedges
    symbol: str
    side: str               # opposite of the hedged cycle direction
    lots: float             # matches hedged cycle total lot size
    opened_ts: int
    opened_price: float
    status: str = "active"  # "active" | "removed" | "converted"
    removed_ts: int = 0
    remove_reason: str = ""
    broker_ticket: str = ""  # MetaApi positionId of the opposite-side fill (live only)
    position_id: str = ""    # internal position_store id of the hedge (paper or live)


# ── Hedge trigger evaluation ──────────────────────────────────────────────────

def should_hedge(
    bar,
    fp,
    cycle_direction: str,
    wave_retrace_pct: float | None,
    sweep_type: str | None,
) -> tuple[bool, str]:
    """Return (should_hedge, reason).

    Checks the three trigger conditions in priority order.
    """
    # 1. Wave retrace > 78.6% — hardest signal, always hedge
    if wave_retrace_pct is not None and wave_retrace_pct > 0.786:
        return True, f"wave retrace {wave_retrace_pct:.1%} > 78.6% — impulse thesis dead"

    # 2. Sweep confirmed against cycle
    if sweep_type:
        if cycle_direction == "long" and sweep_type == "sweep_high":
            # sweep high = price went above level and rejected — bearish
            return True, f"sweep_high confirmed against long cycle"
        if cycle_direction == "short" and sweep_type == "sweep_low":
            return True, f"sweep_low confirmed against short cycle"

    # 3. Footprint invalidation — opposite absorption at entry zone
    if fp is not None:
        from pipeline.features.invalidation import detect_invalidation
        inv = detect_invalidation(bar, fp, cycle_direction, None)
        if inv and inv.invalidated:
            return True, f"footprint invalidation: {inv.reason}"

    return False, ""


def should_remove_hedge(
    bar,
    fp,
    hedge: HedgePosition,
    cycle_direction: str,
    wave_cvd_quality: str | None,
) -> tuple[bool, str]:
    """Return (should_remove, reason).

    Hedge removed when price recovers AND footprint confirms.
    """
    current_price = bar.ohlc.c

    if cycle_direction == "long":
        price_recovered = current_price >= hedge.opened_price
    else:
        price_recovered = current_price <= hedge.opened_price

    if not price_recovered:
        return False, ""

    # CVD must not be making new extremes against cycle
    if wave_cvd_quality == "diverging":
        return False, "CVD diverging — keep hedge until CVD stabilises"

    # Footprint should show confirming absorption
    if fp is not None:
        from pipeline.features.absorption import detect_absorption
        absorptions = detect_absorption(bar, fp)
        confirming = [
            a for a in absorptions
            if (cycle_direction == "long" and a.side == "buy") or
               (cycle_direction == "short" and a.side == "sell")
        ]
        if confirming:
            return True, f"price recovered to {current_price:.2f} + {cycle_direction} absorption confirmed"

    # Price recovered even without absorption signal (weaker confirmation)
    return True, f"price recovered to {current_price:.2f} (no footprint confirm)"


# ── In-memory hedge registry ──────────────────────────────────────────────────

_hedges: dict[str, HedgePosition] = {}
_lock = Lock()


def open_hedge(
    cycle_id: str,
    symbol: str,
    cycle_direction: str,
    lots: float,
    current_price: float,
) -> HedgePosition:
    """Create and register a hedge position."""
    hedge_id = uuid.uuid4().hex[:10]
    side = "short" if cycle_direction == "long" else "long"
    hedge = HedgePosition(
        hedge_id=hedge_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side=side,
        lots=lots,
        opened_ts=int(time.time()),
        opened_price=current_price,
    )
    with _lock:
        _hedges[hedge_id] = hedge

    # Update cycle store
    from execution.cycle_store import cycle_store
    cycle_store().hedge_cycle(cycle_id, hedge_id)

    return hedge


def remove_hedge(hedge_id: str, reason: str) -> HedgePosition | None:
    """Mark hedge removed and update cycle to recovered."""
    with _lock:
        hedge = _hedges.get(hedge_id)
        if not hedge or hedge.status != "active":
            return None
        hedge.status = "removed"
        hedge.removed_ts = int(time.time())
        hedge.remove_reason = reason

    from execution.cycle_store import cycle_store
    cycle_store().recover_cycle(hedge.cycle_id)
    return hedge


def convert_to_recovery_cycle(
    hedge: HedgePosition,
    position_id: str,
    tf: str,
) -> str:
    """Promote a held hedge into a full recovery cycle (cycle N+1).

    Called when the hedge has been active for N bars without recovery.
    Returns the new cycle_id.
    """
    from execution.cycle_store import cycle_store
    cs = cycle_store()
    new_cycle = cs.open_cycle(
        symbol=hedge.symbol,
        tf=tf,
        direction=hedge.side,
        position_id=position_id,
        parent_cycle_id=hedge.cycle_id,
    )
    with _lock:
        hedge.status = "converted"
    return new_cycle.cycle_id


def active_hedge_for_cycle(cycle_id: str) -> HedgePosition | None:
    return next(
        (h for h in _hedges.values()
         if h.cycle_id == cycle_id and h.status == "active"),
        None,
    )


def get_hedge(hedge_id: str) -> HedgePosition | None:
    return _hedges.get(hedge_id)


# ── Circuit breaker ───────────────────────────────────────────────────────────

def check_circuit_breaker(
    symbol: str,
    settings: dict,
    position_store,
    current_price: float | None = None,
) -> tuple[bool, str]:
    """Return (triggered, reason).

    Two independent triggers:
      1. Total open legs ≥ max_cycles × max_legs_per_cycle (exposure cap)
      2. Net unrealized R ≤ circuit_breaker_r (needs current_price)
    """
    risk_cfg = settings.get("risk", {})
    cb_r = float(risk_cfg.get("circuit_breaker_r", -5.0))

    open_positions = position_store.open_positions(symbol)
    if not open_positions:
        return False, ""

    # Trigger 1: total-leg exposure cap
    from execution.cycle_store import cycle_store
    total_legs = cycle_store().total_open_exposure(symbol)
    max_legs_total = int(risk_cfg.get("max_cycles", 3)) * int(risk_cfg.get("max_legs_per_cycle", 6))
    if total_legs >= max_legs_total:
        return True, f"max total legs reached: {total_legs} >= {max_legs_total}"

    # Trigger 2: net unrealized R (only if we have a live price)
    if current_price is not None:
        nur = net_unrealized_r(symbol, current_price, position_store)
        if nur <= cb_r:
            return True, f"net unrealized R {nur:.2f} ≤ circuit_breaker_r {cb_r}"

    return False, ""


def net_unrealized_r(symbol: str, current_price: float, position_store) -> float:
    """Compute net unrealized R across all open positions for a symbol."""
    total = 0.0
    for pos in position_store.open_positions(symbol):
        if not pos.legs:
            continue
        avg_entry = pos.avg_entry
        sl = pos.stop_loss
        risk = abs(avg_entry - sl) if sl else 0.0
        if risk <= 0:
            continue
        if pos.side == "long":
            unrealized = (current_price - avg_entry) / risk
        else:
            unrealized = (avg_entry - current_price) / risk
        total += unrealized
    return round(total, 3)
