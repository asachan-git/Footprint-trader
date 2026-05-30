"""Big Trade Logger — tracks significant volume events and their outcomes.

A "big trade" = a bar where volume at a specific price level exceeds
BIG_TRADE_MULTIPLIER × session average for that level tier.

Every event is logged to data/big_trades.jsonl immediately on detection.
Outcome (pushed / absorbed / exhausted) is classified N bars later via
deferred evaluation, and the JSONL row is updated.

Session memory fed to Claude: last 5 significant events near current price.
Pattern: "3280 absorbed large buys twice this session → strong resistance zone."

Outcome definitions:
  pushed:     Large aggressor buy/sell → price moved >MIN_PUSH_PCT in that
              direction over the next OUTCOME_BARS bars.
  absorbed:   Large aggressor → price did NOT follow through (<MIN_PUSH_PCT).
  exhausted:  Large aggressor at extreme → price REVERSED >MIN_PUSH_PCT
              against the aggressor direction.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

from pipeline.features.absorption import detect_absorption  # existing feature

ROOT = Path(__file__).resolve().parent.parent.parent
BIG_TRADES_LOG = ROOT / "data" / "big_trades.jsonl"

BIG_TRADE_MULTIPLIER = 2.0   # volume > 2× session avg = "big"
OUTCOME_BARS = 3              # classify outcome N bars after the event
MIN_PUSH_PCT = 0.002          # 0.2% price move = "pushed"
MAX_NEAR_PRICE_PCT = 0.005    # 0.5% from current price = "near"

_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


@dataclass
class BigTradeEvent:
    event_id: str
    ts: int
    ts_ist: str
    bar_id: str
    symbol: str
    tf: str
    price: float              # price level where big volume hit
    volume: float             # total volume at that level in the bar
    aggressor: str            # "buy" | "sell"
    vp_context: str           # "at_hvn" | "at_poc" | "at_val" | "at_vah" | "at_lvn" | "at_sweep_zone" | "general"
    outcome: str = "pending"  # "pending" | "pushed" | "absorbed" | "exhausted"
    price_moved_pct: float = 0.0
    outcome_bar_id: str = ""
    sweep_associated: bool = False


def _bar_volume(bar) -> float:
    total = sum(lvl.vol for lvl in bar.bid_ladder) + sum(lvl.vol for lvl in bar.ask_ladder)
    return total or abs(bar.delta or 0.0)


def _session_avg_volume(recent_bars: list) -> float:
    vols = [_bar_volume(b) for b in recent_bars if _bar_volume(b) > 0]
    return sum(vols) / len(vols) if vols else 0.0


def _vp_context_for_price(price: float, daily_vp: dict | None) -> str:
    if not daily_vp:
        return "general"
    poc  = daily_vp.get("poc")
    vah  = daily_vp.get("vah")
    val  = daily_vp.get("val")
    hvns = daily_vp.get("hvn_zones", [])
    lvns = daily_vp.get("lvn_zones", [])

    if poc and abs(price - poc) / max(poc, 1) < 0.001:
        return "at_poc"
    if vah and abs(price - vah) / max(vah, 1) < 0.002:
        return "at_vah"
    if val and abs(price - val) / max(val, 1) < 0.002:
        return "at_val"
    for z in hvns:
        if z["low"] <= price <= z["high"]:
            return "at_hvn"
    for z in lvns:
        if z["low"] <= price <= z["high"]:
            return "at_lvn"
    return "general"


def detect_events(bar, recent_bars: list, daily_vp: dict | None = None) -> list[BigTradeEvent]:
    """Detect big trade events on the given bar.

    Checks each absorption zone detected on the bar and flags those where
    volume at the absorbed price level exceeds BIG_TRADE_MULTIPLIER × session avg.

    Returns list of BigTradeEvent (usually 0-2 per bar).
    """
    avg_vol = _session_avg_volume(recent_bars[-30:] if len(recent_bars) >= 30 else recent_bars)
    if avg_vol <= 0:
        return []

    events: list[BigTradeEvent] = []
    # Use bar close_ts so backfilled / historical detections retain correct
    # timestamps. Fall back to wall-clock only if bar has no close_ts.
    now_ts = int(getattr(bar, "close_ts", 0)) or int(time.time())

    # Use existing absorption detector to find significant prints
    try:
        absorptions = detect_absorption(bar, None)  # type: ignore[arg-type]
    except Exception:
        absorptions = []

    # Also check raw ladder for large single-level prints
    all_levels = list(bar.bid_ladder) + list(bar.ask_ladder)
    if not all_levels:
        return []

    bar_total = _bar_volume(bar)
    level_threshold = avg_vol * BIG_TRADE_MULTIPLIER / max(len(all_levels), 1)

    seen_prices: set[float] = set()
    for lvl in all_levels:
        if lvl.vol < level_threshold:
            continue
        if lvl.price in seen_prices:
            continue
        seen_prices.add(lvl.price)

        # Determine aggressor side for this level
        bid_vol = sum(l.vol for l in bar.bid_ladder if l.price == lvl.price)
        ask_vol = sum(l.vol for l in bar.ask_ladder if l.price == lvl.price)
        aggressor = "buy" if bid_vol >= ask_vol else "sell"

        import uuid
        event = BigTradeEvent(
            event_id=uuid.uuid4().hex[:10],
            ts=now_ts,
            ts_ist=_ts_ist(now_ts),
            bar_id=bar.bar_id,
            symbol=bar.symbol,
            tf=bar.tf,
            price=lvl.price,
            volume=lvl.vol,
            aggressor=aggressor,
            vp_context=_vp_context_for_price(lvl.price, daily_vp),
            outcome="pending",
        )
        events.append(event)

    return events


def log_events(events: list[BigTradeEvent]) -> None:
    """Append new BigTradeEvents to data/big_trades.jsonl."""
    if not events:
        return
    BIG_TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BIG_TRADES_LOG.open("a") as fh:
        for ev in events:
            fh.write(json.dumps(asdict(ev)) + "\n")


def classify_outcomes(recent_bars: list, symbol: str) -> int:
    """Scan pending events and classify their outcome using recent bar prices.

    Called from ingest route on every bar. Returns number of events updated.
    """
    if not BIG_TRADES_LOG.exists():
        return 0

    lines = BIG_TRADES_LOG.read_text().splitlines()
    pending = [(i, json.loads(l)) for i, l in enumerate(lines) if l.strip()]
    pending = [(i, e) for i, e in pending if e.get("outcome") == "pending" and e.get("symbol") == symbol]

    if not pending:
        return 0

    updated = 0
    new_lines = list(lines)

    for idx, event in pending:
        event_ts = event["ts"]
        # Find bars that came AFTER the event
        future_bars = [b for b in recent_bars if b.close_ts > event_ts]
        if len(future_bars) < OUTCOME_BARS:
            continue  # not enough bars yet

        outcome_bars = future_bars[:OUTCOME_BARS]
        entry_price = event["price"]
        aggressor = event["aggressor"]

        high_after = max(b.ohlc.h for b in outcome_bars)
        low_after  = min(b.ohlc.l for b in outcome_bars)

        if aggressor == "buy":
            move_in_direction = (high_after - entry_price) / max(entry_price, 1)
            move_against = (entry_price - low_after) / max(entry_price, 1)
        else:
            move_in_direction = (entry_price - low_after) / max(entry_price, 1)
            move_against = (high_after - entry_price) / max(entry_price, 1)

        if move_against > MIN_PUSH_PCT:
            outcome = "exhausted"
            price_moved = -move_against
        elif move_in_direction > MIN_PUSH_PCT:
            outcome = "pushed"
            price_moved = move_in_direction
        else:
            outcome = "absorbed"
            price_moved = move_in_direction

        event["outcome"] = outcome
        event["price_moved_pct"] = round(price_moved * 100, 4)
        event["outcome_bar_id"] = outcome_bars[-1].bar_id
        new_lines[idx] = json.dumps(event)
        updated += 1

    if updated > 0:
        BIG_TRADES_LOG.write_text("\n".join(new_lines) + "\n")

    return updated


def get_recent_events(symbol: str, tf: str, n: int = 20) -> list[BigTradeEvent]:
    """Return last N BigTradeEvents for symbol/tf with resolved outcomes.

    Reads from BIG_TRADES_LOG. Used by confirmation.py for mechanical scoring.
    """
    if not BIG_TRADES_LOG.exists():
        return []
    result: list[BigTradeEvent] = []
    for line in BIG_TRADES_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if e.get("symbol") == symbol and e.get("tf") == tf and e.get("outcome") != "pending":
                result.append(BigTradeEvent(**{k: e[k] for k in BigTradeEvent.__dataclass_fields__ if k in e}))
        except Exception:
            pass
    return result[-n:]


def session_summary(symbol: str, current_price: float, n: int = 5) -> dict:
    """Return last N big trade events near current price for Claude context.

    Returns:
        {
          "near_current_price": [...],  # events within MAX_NEAR_PRICE_PCT of current
          "session_summary": "3280 absorbed large buys twice..."
        }
    """
    if not BIG_TRADES_LOG.exists():
        return {"near_current_price": [], "session_summary": "no big trade data"}

    events: list[dict] = []
    for line in BIG_TRADES_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if e.get("symbol") == symbol:
                events.append(e)
        except Exception:
            pass

    # Filter to events near current price
    near = [
        e for e in events
        if abs(e["price"] - current_price) / max(current_price, 1) <= MAX_NEAR_PRICE_PCT
    ]
    near = sorted(near, key=lambda e: e["ts"])[-n:]

    # Build summary sentence
    absorbed = [e for e in near if e["outcome"] == "absorbed"]
    pushed   = [e for e in near if e["outcome"] == "pushed"]
    exhausted = [e for e in near if e["outcome"] == "exhausted"]

    parts: list[str] = []
    if absorbed:
        prices = sorted(set(round(e["price"], 1) for e in absorbed))
        parts.append(f"{prices} absorbed ({len(absorbed)}x)")
    if pushed:
        prices = sorted(set(round(e["price"], 1) for e in pushed))
        parts.append(f"{prices} pushed through")
    if exhausted:
        prices = sorted(set(round(e["price"], 1) for e in exhausted))
        parts.append(f"{prices} exhausted (sweep/reversal)")

    summary = " | ".join(parts) if parts else "no resolved events near price"

    return {
        "near_current_price": [
            {
                "price": e["price"],
                "aggressor": e["aggressor"],
                "outcome": e["outcome"],
                "vp_context": e["vp_context"],
                "ts_ist": e.get("ts_ist", ""),
            }
            for e in near
        ],
        "session_summary": summary,
    }
