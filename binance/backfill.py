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


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int,
                     page_size: int = 1000, max_retries: int = 3) -> list[dict]:
    """Fetch all aggTrades for `symbol` in [start_ms, end_ms). Paginates in ≤1h sub-windows
    (Binance caps a single query's span). Returns [] on persistent error (fail-safe: a heal
    that can't fetch degrades to the pre-heal behaviour, it never raises into the caller)."""
    trades: list[dict] = []
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
                if attempt == max_retries - 1:
                    LOG.warning(f"[backfill] aggTrades fetch failed at {cursor} "
                                f"({symbol}) after {max_retries} tries: {e}")
                    batch = None
                else:
                    time.sleep(2)
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
        time.sleep(0.05)  # stay under the rate limit
    return trades


def backfill_window(binance_symbol: str, store_symbol: str, start_ms: int, end_ms: int,
                    tf: str = "1m", price_step: float = 0.1,
                    source: str = "binance_agg_backfill", replace: bool = False) -> int:
    """Fetch aggTrades in [start_ms, end_ms), replay through FootprintBuilder, inject into
    the state_store. Returns the count of NEW bars injected (dupes dropped by store.put).

    binance_symbol: the Binance fetch symbol (e.g. XAUUSDT).
    store_symbol:   the key it's stored under (e.g. XAUTUSDT) — bar_id is recomputed for it.
    replace: when True, first DELETE any existing bars in the window so a WRONG existing bar
      is overwritten (put() is idempotent and won't replace). Used by the feed-reconcile heal
      path — keep the window tight (only the breached candle's minutes). Default False =
      gap-heal semantics (fill missing only, never touch good bars).
    Bounded: the caller clamps the [start,end) span; this replays whatever it's given.
    Safe to overlap the live feed (idempotent + Lock-guarded store)."""
    s = store()
    if replace:
        removed = s.delete_range(store_symbol, tf, start_ms // 1000, (end_ms + 999) // 1000)
        if removed:
            LOG.info(f"[backfill] replace: evicted {removed} existing {store_symbol} {tf} "
                     f"bars in [{start_ms // 1000}, {end_ms // 1000}) before re-fetch")
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
