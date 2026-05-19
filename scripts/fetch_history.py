"""Download Bybit 1m OHLCV history and inject into state_store as synthetic bars.

NOTE: OHLCV bars don't have per-price bid/ask ladders — footprint matrix will be
empty. However, VP (POC, VAH, VAL, shape) is computed from price×volume distribution
using the bar's total volume, which gives a reasonable approximation for context.

Usage:
  python3 scripts/fetch_history.py --symbol BTCUSDT --days 5
  python3 scripts/fetch_history.py --symbol XAUTUSDT --days 5 --category spot
  python3 scripts/fetch_history.py --symbol BTCUSDT --symbol XAUTUSDT --days 5
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.types import Bar, Level, OHLC
from pipeline.state_store import store
from pipeline.features.vp_cache import build_and_save

IST_TZ = timezone(timedelta(hours=5, minutes=30))
SESSION_START_UTC = {
    "BTCUSDT": 0,
    "XAUTUSDT": 22,
}


def _bar_id(symbol: str, tf: str, close_ts: int) -> str:
    h = hashlib.sha1(f"{symbol}|{tf}|{close_ts}".encode()).hexdigest()[:16]
    return f"{symbol}|{tf}|{close_ts}|{h}"


def fetch_bybit_klines(
    symbol: str,
    category: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1",
) -> list[dict]:
    """Fetch Bybit klines between start_ms and end_ms. Returns list of {ts, o, h, l, c, vol}."""
    url = "https://api.bybit.com/v5/market/kline"
    all_bars = []
    cursor_end = end_ms
    while True:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "start": start_ms,
            "end": cursor_end,
            "limit": 1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  API error: {e}")
            break
        if data.get("retCode") != 0:
            print(f"  API error: {data}")
            break
        bars = data.get("result", {}).get("list", [])
        if not bars:
            break
        # Each bar: [startTime, open, high, low, close, volume, turnover]
        for b in bars:
            bar_ts_ms = int(b[0])
            close_ts = bar_ts_ms // 1000 + 60  # 1m bar close = open + 60s
            all_bars.append({
                "ts": close_ts,
                "o": float(b[1]),
                "h": float(b[2]),
                "l": float(b[3]),
                "c": float(b[4]),
                "vol": float(b[5]),
            })
        oldest = int(bars[-1][0])
        if oldest <= start_ms:
            break
        cursor_end = oldest - 1
        time.sleep(0.1)
    return sorted(all_bars, key=lambda x: x["ts"])


def _build_synthetic_bar(symbol: str, tf: str, b: dict, price_step: float = 10.0) -> Bar:
    """Build Bar from OHLCV. Volume distributed across price range for approximate VP.

    Since we don't have per-price bid/ask, we spread volume evenly across
    the bar's price range at price_step granularity. Ask = vol/2, bid = vol/2.
    This gives a flat distribution within the bar — POC = midpoint.

    Better than nothing for VP context; clearly marked as 'synthetic'.
    """
    lo, hi = min(b["l"], b["o"]), max(b["h"], b["c"])
    levels = []
    p = round(lo / price_step) * price_step
    while p <= hi + price_step:
        levels.append(p)
        p = round((p + price_step) / price_step) * price_step
    rng = b["h"] - b["l"]
    delta = b["vol"] * (2.0 * (b["c"] - b["l"]) / rng - 1.0) if rng > 0 else (b["vol"] if b["c"] >= b["o"] else -b["vol"])
    if not levels:
        levels = [b["c"]]
    n_levels = len(levels)
    rng_l = b["h"] - b["l"]
    # Weight each level by proximity to close — gaussian-like (triangular)
    weights = []
    for p in levels:
        dist = abs(p - b["c"]) / (rng_l + 1e-9)
        weights.append(max(0.1, 1.0 - dist * 1.5))
    total_w = sum(weights) or 1.0
    # Bullish bar: more ask vol at low levels (buyers lifting offers), more bid at highs
    # Bearish bar: reversed. Split 60/40 directionally.
    is_bull = b["c"] >= b["o"]
    bid_ladder_levels = []
    ask_ladder_levels = []
    for p, w in zip(levels, weights):
        level_vol = b["vol"] * w / total_w
        price_pos = (p - b["l"]) / (rng_l + 1e-9)  # 0=low, 1=high
        if is_bull:
            ask_frac = max(0.2, 0.7 - price_pos * 0.5)  # more ask at bottom
            bid_frac = 1.0 - ask_frac
        else:
            bid_frac = max(0.2, 0.7 - price_pos * 0.5)  # more bid at bottom for bears
            ask_frac = 1.0 - bid_frac
        bid_ladder_levels.append(Level(price=p, vol=level_vol * bid_frac))
        ask_ladder_levels.append(Level(price=p, vol=level_vol * ask_frac))
    bid_ladder = tuple(bid_ladder_levels)
    ask_ladder = tuple(ask_ladder_levels)
    return Bar(
        bar_id=_bar_id(symbol, tf, b["ts"]),
        symbol=symbol,
        tf=tf,
        close_ts=b["ts"],
        source="synthetic_ohlcv",
        ohlc=OHLC(o=b["o"], h=b["h"], l=b["l"], c=b["c"]),
        bid_ladder=bid_ladder,
        ask_ladder=ask_ladder,
        delta=round(delta, 4),
        poc=None,
    )


def download_and_store(
    symbol: str,
    days: int,
    category: str,
    price_step: float,
    session_start_utc: int,
) -> int:
    """Download last N days of 1m bars and inject into state_store. Returns count injected."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    ist_now = datetime.fromtimestamp(time.time(), tz=IST_TZ)
    print(f"  Fetching {symbol} {days}d of 1m klines from Bybit ({category})...")
    print(f"  Range: {datetime.fromtimestamp(start_ms/1000, tz=IST_TZ).strftime('%Y-%m-%d %H:%M IST')} → now")

    raw = fetch_bybit_klines(symbol, category, start_ms, now_ms)
    if not raw:
        print(f"  No data returned.")
        return 0

    print(f"  Downloaded {len(raw)} bars from Bybit")

    s = store()
    injected = 0
    for b in raw:
        bar = _build_synthetic_bar(symbol, "1m", b, price_step)
        if s.put(bar):
            injected += 1

    print(f"  Injected {injected} new bars into state_store")
    return injected


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", action="append", default=[])
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--category", default=None,
                    help="linear|spot (auto-detected if not set)")
    ap.add_argument("--price-step", type=float, default=None,
                    help="Price step for synthetic bid/ask bucketing")
    args = ap.parse_args()

    symbols = args.symbol or ["BTCUSDT", "XAUTUSDT"]
    category_map = {"BTCUSDT": "linear", "XAUTUSDT": "spot"}
    step_map = {"BTCUSDT": 10.0, "XAUTUSDT": 0.1}
    session_map = SESSION_START_UTC

    total = 0
    for sym in symbols:
        cat = args.category or category_map.get(sym, "linear")
        step = args.price_step or step_map.get(sym, 10.0)
        sess = session_map.get(sym, 0)
        print(f"\n[{sym}]")
        n = download_and_store(sym, args.days, cat, step, sess)
        total += n

    if total > 0:
        print(f"\nRefreshing VP cache for {symbols}...")
        build_and_save(symbols, "1m", session_start_utc=SESSION_START_UTC)  # type: ignore[call-arg]
        print("VP cache updated. Run verify_vp.py to check.")
    else:
        print("\nNo new bars injected. Cache not updated.")


if __name__ == "__main__":
    main()
