"""Compare Binance aggTrade vs ByBit footprint data for the same symbol/window.

Fetches Binance data in-memory (no write to state_store), replays through
FootprintBuilder, then compares bar-by-bar against existing ByBit bars.

Usage:
  python3 scripts/compare_feeds.py --symbol BTCUSDT --hours 6
  python3 scripts/compare_feeds.py --symbol XAUTUSDT --hours 6 --binance-symbol XAUUSDT
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from collections import defaultdict

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bybit.footprint_builder import FootprintBuilder
from pipeline.state_store import store as _store

BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
BINANCE_SPOT_URL    = "https://api.binance.com/api/v3/aggTrades"
BINANCE_URL = BINANCE_SPOT_URL  # default; overridden per symbol in fetch_trades


SPOT_SYMBOLS = {"XAUTUSDT", "XAUTUSDC"}


def fetch_trades(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    url = BINANCE_SPOT_URL if symbol in SPOT_SYMBOLS else BINANCE_FUTURES_URL
    trades = []
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + 300_000, end_ms)  # 5-min windows
        for attempt in range(4):
            resp = requests.get(url, params={
                "symbol": symbol, "startTime": cursor,
                "endTime": window_end, "limit": 1000,
            }, timeout=15)
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"  rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        page = resp.json()
        if not page:
            cursor = window_end + 1
            continue
        trades.extend(page)
        cursor = int(page[-1]["T"]) + 1
        time.sleep(0.05)  # ~20 req/s — well inside Binance 500 req/5min limit
    return trades


def build_bn_bars(binance_symbol: str, trades: list[dict], price_step: float) -> dict[int, dict]:
    bars: dict[int, dict] = {}

    def collect(payload: dict) -> None:
        bars[payload["close_ts"]] = payload

    builder = FootprintBuilder(binance_symbol, "1m", on_bar_close=collect, price_step=price_step)
    for t in trades:
        side = "Sell" if t["m"] else "Buy"  # isBuyerMaker=True → seller taker → bid
        builder.on_tick(ts_ms=int(t["T"]), price=float(t["p"]), size=float(t["q"]), side=side)
    builder.flush()
    return bars


def compare(args):
    hours = args.hours
    symbol = args.symbol
    binance_symbol = args.binance_symbol or symbol
    price_step = args.price_step

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    cutoff_s = now_ms // 1000 - hours * 3600

    print(f"Fetching {hours}h Binance aggTrades for {binance_symbol}...")
    trades = fetch_trades(binance_symbol, start_ms, now_ms)
    span_s = (int(trades[-1]["T"]) - int(trades[0]["T"])) / 1000 if trades else 0
    print(f"  Got {len(trades):,} trades spanning {span_s/3600:.2f}h")

    print(f"Building Binance bars...")
    bn_bars = build_bn_bars(binance_symbol, trades, price_step)
    print(f"  Built {len(bn_bars)} bars")

    print(f"Loading ByBit bars from state_store ({symbol})...")
    s = _store()
    bb_all = s.recent(symbol, "1m", 100_000)
    bb_bars = {b.close_ts: b for b in bb_all if b.close_ts >= cutoff_s}
    print(f"  Got {len(bb_bars)} bars")

    # Find overlap
    common_ts = sorted(set(bn_bars) & set(bb_bars))
    print(f"  Overlapping bars: {len(common_ts)}\n")

    if not common_ts:
        print("No overlapping bars found. Check symbol/time range.")
        return

    # Per-bar comparison
    delta_agree = delta_disagree = delta_both_zero = 0
    vol_diffs = []
    close_diffs = []
    large_vol_diff = []

    header = f"{'ts':>12}  {'BN_delta':>9}  {'BB_delta':>9}  {'sign':>5}  {'BN_vol':>9}  {'BB_vol':>9}  {'vol%diff':>8}  {'close%diff':>10}"
    print(header)
    print("-" * len(header))

    for ts in common_ts:
        bn = bn_bars[ts]
        bb = bb_bars[ts]

        bn_d = bn.get("delta") or 0.0
        bb_d = bb.delta or 0.0

        if bn_d != 0 and bb_d != 0:
            agree = (bn_d > 0) == (bb_d > 0)
            sign_str = "✓" if agree else "✗"
            if agree:
                delta_agree += 1
            else:
                delta_disagree += 1
        else:
            sign_str = "—"
            delta_both_zero += 1

        bn_vol = sum(l["vol"] for l in bn.get("bid_ladder", []) + bn.get("ask_ladder", []))
        bb_vol = sum(l.vol for l in bb.bid_ladder + bb.ask_ladder)
        vol_diff_pct = abs(bn_vol - bb_vol) / max(bb_vol, 1e-9) * 100
        vol_diffs.append(vol_diff_pct)

        bn_c = bn["ohlc"]["c"]
        bb_c = bb.ohlc.c
        close_diff_pct = abs(bn_c - bb_c) / bb_c * 100
        close_diffs.append(close_diff_pct)

        if vol_diff_pct > 30:
            large_vol_diff.append((ts, vol_diff_pct, bn_d, bb_d))

        print(f"{ts:>12}  {bn_d:>+9.2f}  {bb_d:>+9.2f}  {sign_str:>5}  {bn_vol:>9.2f}  {bb_vol:>9.2f}  {vol_diff_pct:>7.1f}%  {close_diff_pct:>9.4f}%")

    total_voted = delta_agree + delta_disagree
    print()
    print("=" * 60)
    print(f"SUMMARY — {len(common_ts)} overlapping bars")
    print(f"  Delta sign agree  : {delta_agree}/{total_voted} ({delta_agree/max(total_voted,1)*100:.0f}%)")
    print(f"  Delta sign disagree: {delta_disagree}/{total_voted} ({delta_disagree/max(total_voted,1)*100:.0f}%)")
    print(f"  Both-zero bars    : {delta_both_zero}")
    if vol_diffs:
        print(f"  Avg vol diff      : {sum(vol_diffs)/len(vol_diffs):.1f}%")
        print(f"  Median vol diff   : {sorted(vol_diffs)[len(vol_diffs)//2]:.1f}%")
        print(f"  Bars with >30% vol diff: {len(large_vol_diff)}")
    if close_diffs:
        print(f"  Avg close diff    : {sum(close_diffs)/len(close_diffs):.4f}%")
        print(f"  Max close diff    : {max(close_diffs):.4f}%")
    print()
    print("INTERPRETATION:")
    agree_pct = delta_agree / max(total_voted, 1) * 100
    avg_vol = sum(vol_diffs) / len(vol_diffs) if vol_diffs else 0
    if agree_pct >= 85:
        print(f"  Delta sign: GOOD ({agree_pct:.0f}% agreement)")
    elif agree_pct >= 70:
        print(f"  Delta sign: ACCEPTABLE ({agree_pct:.0f}% agreement) — some noise expected")
    else:
        print(f"  Delta sign: POOR ({agree_pct:.0f}% agreement) — investigate feed quality")

    if avg_vol > 50:
        print(f"  Volume: HIGH DIFF ({avg_vol:.0f}% avg) — different participant bases (Binance larger)")
    elif avg_vol > 20:
        print(f"  Volume: MODERATE DIFF ({avg_vol:.0f}% avg) — expected cross-venue variation")
    else:
        print(f"  Volume: LOW DIFF ({avg_vol:.0f}% avg) — feeds are well-aligned")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--binance-symbol", default=None, help="Binance symbol if different (e.g. XAUUSDT for XAUTUSDT)")
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--price-step", type=float, default=1.0)
    compare(ap.parse_args())
