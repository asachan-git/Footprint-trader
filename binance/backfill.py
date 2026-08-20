"""Bounded Binance aggTrades backfill → real footprint bars in the state_store.

Shared core for two callers:
  - scripts/backfill_binance.py — CLI, `--days N` cold-start backfill.
  - pipeline/feed_monitor.py    — live self-heal: on feed RECOVERED, fetch exactly the
    missed window so the 1m series has no hole (see the 2026-07-15 gap audit — 62 real
    intra-session gaps, ~22h, that nothing ever backfilled).

aggTrades (not klines) so the rebuilt bars carry GENUINE order flow — real bid/ask ladders,
delta, POC — not OHLC-only. `store().put()` is idempotent (dedups by bar_id) and rejects
sentinel/far-future timestamps, so overlapping the live feed at the gap boundary is safe;
`store` is a Lock-guarded singleton, so concurrent live + backfill writes don't race.

Binance aggTrades retention is ~30d — a start older than that returns empty (log + skip).
"""

from __future__ import annotations

import logging
import time

import requests

from bybit.footprint_builder import FootprintBuilder, _bar_id
from pipeline.state_store import store
from pipeline.types import Bar, Level, OHLC

LOG = logging.getLogger(__name__)

BINANCE_FUTURES_AGG_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
_HOUR_MS = 3_600_000
# aggTrades serves only the recent ~2 days; keep a margin under the documented limit.
_RETENTION_MS = 47 * 3_600_000
# 429/418 get a much longer retry budget than a real error: being throttled is the
# server pacing us, not a fault, and skipping the window would leave a hole.
_THROTTLE_RETRIES = 8
_PACE_S = 0.35        # 0.05 was too hot for a multi-hour heal and drew 429s


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int,
                     page_size: int = 1000, max_retries: int = 3) -> list[dict]:
    """Fetch all aggTrades for `symbol` in [start_ms, end_ms). Paginates in ≤1h sub-windows
    (Binance caps a single query's span). Returns [] on persistent error (fail-safe: a heal
    that can't fetch degrades to the pre-heal behaviour, it never raises into the caller)."""
    trades: list[dict] = []
    # Clamp to the retention window rather than grinding through days of windows
    # that can only 400. Without this a multi-day heal burns max_retries per hour
    # of history — hundreds of doomed requests — and looks like a network fault
    # in the log rather than what it is.
    oldest = int(time.time() * 1000) - _RETENTION_MS
    if end_ms <= oldest:
        LOG.warning("[backfill] %s window [%d, %d) is entirely older than the aggTrades "
                    "retention (~%dh) — cannot heal with real footprint",
                    symbol, start_ms, end_ms, _RETENTION_MS // 3_600_000)
        return []
    if start_ms < oldest:
        LOG.warning("[backfill] %s start clamped %d -> %d (aggTrades retention ~%dh); "
                    "the earlier %.1fh of this gap cannot be healed from this endpoint",
                    symbol, start_ms, oldest, _RETENTION_MS // 3_600_000,
                    (oldest - start_ms) / 3_600_000)
        start_ms = oldest
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + _HOUR_MS, end_ms)
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    BINANCE_FUTURES_AGG_URL,
                    params={"symbol": symbol, "startTime": cursor,
                            "endTime": window_end, "limit": page_size},
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                break
            except Exception as e:
                # A 429 is not a failure, it is the server asking us to slow down.
                # Retrying it three times two seconds apart and then SKIPPING the
                # window silently punches a hole in the heal — the exact thing this
                # module exists to prevent. Back off exponentially and honour
                # Retry-After, with a much longer budget for throttling than for a
                # genuine error.
                status = getattr(getattr(e, "response", None), "status_code", None)
                throttled = status in (418, 429)
                budget = _THROTTLE_RETRIES if throttled else max_retries
                if attempt >= budget - 1:
                    LOG.warning(f"[backfill] aggTrades fetch failed at {cursor} "
                                f"({symbol}) after {attempt + 1} tries: {e}")
                    batch = None
                else:
                    if throttled:
                        hdr = getattr(getattr(e, "response", None), "headers", {}) or {}
                        wait = float(hdr.get("Retry-After") or 0) or min(60.0, 2.0 * (2 ** attempt))
                        LOG.info("[backfill] throttled (%s) — sleeping %.0fs", status, wait)
                    else:
                        wait = 2.0
                    time.sleep(wait)
        if batch is None:
            # Skip this sub-window rather than abort the whole heal.
            cursor = window_end
            continue
        if not batch:
            cursor = window_end
            continue
        trades.extend(batch)
        last_ts = int(batch[-1]["T"])
        # Advance past the last trade; if a full page landed inside the sub-window, keep
        # paging from last_ts+1 so we don't drop trades beyond the 1000-row page.
        cursor = max(last_ts + 1, cursor + 1) if last_ts < window_end else window_end
        time.sleep(_PACE_S)   # stay under the weight limit (aggTrades limit=1000 is weight 20)
    return trades


def backfill_window(binance_symbol: str, store_symbol: str, start_ms: int, end_ms: int,
                    tf: str = "1m", price_step: float = 0.1,
                    source: str = "binance_agg_backfill") -> int:
    """Fetch aggTrades in [start_ms, end_ms), replay through FootprintBuilder, inject into
    the state_store. Returns the count of NEW bars injected (dupes dropped by store.put).

    binance_symbol: the Binance fetch symbol (e.g. XAUUSDT).
    store_symbol:   the key it's stored under (e.g. XAUTUSDT) — bar_id is recomputed for it.
    Bounded: the caller clamps the [start,end) span; this replays whatever it's given.
    Safe to overlap the live feed (idempotent + Lock-guarded store)."""
    s = store()
    injected = 0

    def _on_bar_close(payload: dict) -> None:
        nonlocal injected
        close_ts = int(payload["close_ts"])
        ohlc = payload["ohlc"]
        bar = Bar(
            bar_id=_bar_id(store_symbol, tf, close_ts),   # recompute for the store symbol
            symbol=store_symbol,
            tf=tf,
            close_ts=close_ts,
            source=source,
            ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
            bid_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"]))
                             for l in payload.get("bid_ladder", [])),
            ask_ladder=tuple(Level(price=float(l["price"]), vol=float(l["vol"]))
                             for l in payload.get("ask_ladder", [])),
            poc=payload.get("poc"),
            delta=float(payload["delta"]) if payload.get("delta") is not None else None,
        )
        if s.put(bar):   # idempotent + sentinel-guarded
            injected += 1

    builder = FootprintBuilder(symbol=binance_symbol, tf=tf,
                               on_bar_close=_on_bar_close, price_step=price_step)
    trades = fetch_agg_trades(binance_symbol, start_ms, end_ms)
    for t in trades:
        side = "Sell" if t["m"] else "Buy"   # m=isBuyerMaker: True → seller is the taker
        builder.on_tick(int(t["T"]), float(t["p"]), float(t["q"]), side)
    builder.flush()   # emit the final partial bar
    return injected
