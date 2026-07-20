"""Aggregate lower-TF bars into higher-TF bars.

Used when ingress emits only the primary TF — we synthesize 5m / 15m
footprints by merging consecutive primary-TF bars' ladders.
"""

from __future__ import annotations

import re

from .types import Bar, Level, OHLC

# Fast path for the common TFs; anything else is parsed. Kept as a dict so the
# hot aggregation loop avoids a regex on every bar.
_TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "1h": 3600}

_TF_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def tf_seconds(tf: str) -> int:
    """Seconds in one bar of `tf` ("3m" → 180, "10m" → 600, "1h" → 3600).

    Parses ANY <int><unit> timeframe rather than looking up a fixed table — the
    old bare `_TF_SECONDS[tf]` raised KeyError for every unlisted TF, so adding
    a timeframe to instrument.timeframes crashed the aggregator on every bar.
    Raises ValueError (not KeyError) with the offending value on a bad TF.
    """
    sec = _TF_SECONDS.get(tf)
    if sec is not None:
        return sec
    m = _TF_RE.match(str(tf).strip().lower())
    if not m:
        raise ValueError(f"unparseable timeframe {tf!r} (expected e.g. '3m', '10m', '1h')")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"timeframe {tf!r} must be positive")
    return n * _UNIT_SECONDS[unit]


def bucket_close(close_ts: int, target_tf: str) -> int:
    """Round UP to the nearest multiple of target_tf seconds.

    A primary bar with close_ts exactly on a target-TF boundary belongs to the
    bucket that closes at that same boundary, not the next one.
    """
    sec = tf_seconds(target_tf)
    return ((close_ts + sec - 1) // sec) * sec


def _merge(bars: list[Bar], target_tf: str) -> Bar:
    first = bars[0]
    by_price_bid: dict[float, float] = {}
    by_price_ask: dict[float, float] = {}
    for b in bars:
        for lvl in b.bid_ladder:
            by_price_bid[lvl.price] = by_price_bid.get(lvl.price, 0.0) + lvl.vol
        for lvl in b.ask_ladder:
            by_price_ask[lvl.price] = by_price_ask.get(lvl.price, 0.0) + lvl.vol
    ohlc = OHLC(
        o=first.ohlc.o,
        h=max(b.ohlc.h for b in bars),
        l=min(b.ohlc.l for b in bars),
        c=bars[-1].ohlc.c,
    )
    close_ts = bucket_close(bars[-1].close_ts, target_tf)

    # Compute delta + POC from merged ladders
    prices = set(by_price_bid) | set(by_price_ask)
    total_bid = sum(by_price_bid.values())
    total_ask = sum(by_price_ask.values())
    delta = total_ask - total_bid
    poc = max(prices, key=lambda p: by_price_bid.get(p, 0.0) + by_price_ask.get(p, 0.0)) if prices else None

    return Bar(
        bar_id=f"{first.symbol}|{target_tf}|{close_ts}",
        symbol=first.symbol,
        tf=target_tf,
        close_ts=close_ts,
        source=first.source,
        ohlc=ohlc,
        bid_ladder=tuple(Level(p, v) for p, v in sorted(by_price_bid.items())),
        ask_ladder=tuple(Level(p, v) for p, v in sorted(by_price_ask.items())),
        poc=poc,
        delta=delta,
    )


def maybe_emit(store_recent: list[Bar], primary_tf: str, target_tf: str) -> Bar | None:
    """Emit a synthesized target_tf bar iff the latest primary bar closes a target bucket.

    Caller passes `store_recent` (chronologically ordered primary-TF bars). If the
    last bar's close_ts is a multiple of `tf_seconds(target_tf)`, gather all
    primary-TF bars in that bucket and merge.
    """
    if not store_recent:
        return None
    # Ignore forming/sentinel bars (a partial-bar placeholder is stored with a max ts like
    # 9999999999 so it sorts last). Such a bar as store_recent[-1] made the boundary check
    # `close_ts % target_sec` never hit 0 → maybe_emit returned None forever → 5m/15m
    # aggregation silently froze while 1m kept flowing (observed 2026-07-07: 5m stuck ~2h,
    # detector ran on stale zones, sweeps never seen). Aggregate off CLOSED bars only.
    _closed = [b for b in store_recent if b.tf == primary_tf and b.close_ts < 9_999_999_990]
    if not _closed:
        return None
    latest = _closed[-1]
    target_sec = tf_seconds(target_tf)
    if (latest.close_ts % target_sec) != 0:
        return None
    bucket_start = latest.close_ts - target_sec
    bucket = [b for b in _closed if bucket_start < b.close_ts <= latest.close_ts]
    if not bucket:
        return None
    return _merge(bucket, target_tf)
