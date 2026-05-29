"""A/B Analysis: Mode 1 (Claude) vs Mode 2 (rules) — achieved vs shown RR per instrument.

Mode 1: actual positions.jsonl open+close events (real outcomes).
Mode 2: price-walk-forward simulation using Bybit historical OHLC.
  - Entry  = bar close at signal time
  - ATR_15m = 14-bar mean(H-L) on 1m bars × sqrt(15)
  - SL     = 5 × ATR_15m (mean-rev default from grid_modes)
  - TP     = 1.5 × ATR_15m (POC/fallback target from grid_modes)
  - Walk   = check each subsequent 1m bar (wick) for TP or SL touch
  - Timeout = 300 bars (5h); close at bar close, partial RR

Usage:
  python scripts/ab_analysis.py [--symbol BTCUSDT|XAUTUSDT] [--last N] [--no-fetch]
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))

# Grid simulation parameters (from grid_modes.py mean_reversion defaults)
SL_ATR_MULT   = 5.0   # SL distance = 5 × ATR_15m
TP_ATR_MULT   = 1.5   # TP distance = 1.5 × ATR_15m (POC/fallback)
ATR_PERIOD    = 14    # bars for ATR
WALK_LIMIT    = 300   # max bars to walk forward (5h on 1m)
SHOWN_RR      = round(TP_ATR_MULT / SL_ATR_MULT, 2)   # 0.30

# Bybit kline config per symbol
BYBIT_CFG: dict[str, dict] = {
    "BTCUSDT":  {"symbol": "BTCUSDT",  "category": "linear"},
    "XAUTUSDT": {"symbol": "XAUTUSDT", "category": "spot"},
}


# ── OHLC fetch ─────────────────────────────────────────────────────────────────

def _fetch_klines(symbol: str, category: str, start_ts: int, end_ts: int) -> list[dict]:
    """Fetch 1m klines from Bybit covering [start_ts, end_ts] (Unix seconds).
    Returns sorted list of {ts, o, h, l, c} ascending by ts.
    """
    bars: list[dict] = []
    end_ms = end_ts * 1000
    start_ms = start_ts * 1000

    # Bybit returns newest first; paginate backward using `end` param
    while True:
        url = (f"https://api.bybit.com/v5/market/kline"
               f"?symbol={symbol}&category={category}&interval=1&limit=1000&end={end_ms}")
        try:
            req = urlreq.Request(url, headers={"User-Agent": "FootprintBiot-ab/1.0"})
            resp = urlreq.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except Exception as e:
            print(f"  [fetch] {symbol} request failed: {e}")
            break

        if data.get("retCode") != 0:
            print(f"  [fetch] {symbol} API error: {data.get('retMsg')}")
            break

        rows = data["result"]["list"]
        if not rows:
            break

        for r in rows:
            ts_ms = int(r[0])
            if ts_ms < start_ms:
                continue
            bars.append({
                "ts": ts_ms // 1000,
                "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]),
            })

        oldest_ms = int(rows[-1][0])
        if oldest_ms <= start_ms:
            break
        end_ms = oldest_ms - 1
        time.sleep(0.25)

    bars.sort(key=lambda b: b["ts"])
    return bars


def fetch_ohlc(symbols: list[str], start_ts: int, end_ts: int,
               cache_dir: Path, no_fetch: bool = False) -> dict[str, list[dict]]:
    """Return {symbol: [bar, ...]} using cache if fresh (<2h old)."""
    result: dict[str, list[dict]] = {}
    cache_dir.mkdir(parents=True, exist_ok=True)

    for sym in symbols:
        cfg = BYBIT_CFG.get(sym)
        if not cfg:
            print(f"  [ohlc] no Bybit config for {sym}, skipping")
            continue

        cache_file = cache_dir / f"ohlc_{sym}_1m.json"
        age = time.time() - cache_file.stat().st_mtime if cache_file.exists() else 9999
        if not no_fetch and age > 7200:  # refetch if >2h old
            print(f"  [ohlc] fetching {sym} ({cfg['category']}) … ", end="", flush=True)
            bars = _fetch_klines(cfg["symbol"], cfg["category"], start_ts - 1800, end_ts + 1800)
            cache_file.write_text(json.dumps(bars))
            print(f"{len(bars)} bars cached")
        else:
            bars = json.loads(cache_file.read_text()) if cache_file.exists() else []
            print(f"  [ohlc] {sym}: {len(bars)} bars from cache ({age/3600:.1f}h old)")

        result[sym] = bars
    return result


# ── ATR + simulation ───────────────────────────────────────────────────────────

def _atr(bars: list[dict], period: int = ATR_PERIOD) -> float:
    """Simple ATR: mean of (H-L) for last `period` bars."""
    if len(bars) < period:
        return max((b["h"] - b["l"]) for b in bars) if bars else 1.0
    recent = bars[-period:]
    return sum(b["h"] - b["l"] for b in recent) / period


def simulate_signal(
    side: str,
    entry_bar_idx: int,
    bars: list[dict],
) -> dict:
    """Walk forward from entry_bar_idx, return outcome dict."""
    if entry_bar_idx < ATR_PERIOD or entry_bar_idx >= len(bars):
        return {"outcome": "no_data", "achieved_rr": None, "bars_held": 0}

    entry_bar = bars[entry_bar_idx]
    prior_bars = bars[entry_bar_idx - ATR_PERIOD: entry_bar_idx]
    atr_1m = _atr(prior_bars)
    atr_15m = atr_1m * math.sqrt(15)

    entry = entry_bar["c"]
    sl_dist = SL_ATR_MULT * atr_15m
    tp_dist = TP_ATR_MULT * atr_15m

    if side == "long":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    for i, bar in enumerate(bars[entry_bar_idx + 1: entry_bar_idx + 1 + WALK_LIMIT], 1):
        if side == "long":
            if bar["l"] <= sl:
                return {"outcome": "sl_hit", "achieved_rr": -1.0, "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "atr_15m": atr_15m}
            if bar["h"] >= tp:
                rr = tp_dist / sl_dist
                return {"outcome": "tp_hit", "achieved_rr": round(rr, 2), "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "atr_15m": atr_15m}
        else:  # short
            if bar["h"] >= sl:
                return {"outcome": "sl_hit", "achieved_rr": -1.0, "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "atr_15m": atr_15m}
            if bar["l"] <= tp:
                rr = tp_dist / sl_dist
                return {"outcome": "tp_hit", "achieved_rr": round(rr, 2), "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "atr_15m": atr_15m}

    # Timeout — close at last bar
    last = bars[min(entry_bar_idx + WALK_LIMIT, len(bars) - 1)]
    close_price = last["c"]
    if side == "long":
        rr = (close_price - entry) / sl_dist
    else:
        rr = (entry - close_price) / sl_dist
    return {"outcome": "timeout", "achieved_rr": round(rr, 2), "bars_held": WALK_LIMIT,
            "entry": entry, "sl": sl, "tp": tp, "atr_15m": atr_15m}


# ── Data loading ───────────────────────────────────────────────────────────────

def load_m1_trades(symbol_filter=None, path: Path | None = None) -> list[dict]:
    ppath = path or (ROOT / "data" / "positions.jsonl")
    rows = [json.loads(l) for l in ppath.open() if l.strip()]
    opens = {r["position_id"]: r for r in rows if r.get("type") == "open"}
    closes = {r["position_id"]: r for r in rows if r.get("type") == "close"}

    trades = []
    for pid, o in opens.items():
        c = closes.get(pid)
        if not c:
            continue
        sym = o.get("symbol", "")
        if symbol_filter and sym != symbol_filter:
            continue
        entry = float(o.get("entry") or 0)
        sl = float(o.get("stop_loss") or 0)
        tp = float(o.get("take_profit") or 0)
        risk = abs(entry - sl) if entry and sl else 0
        reward = abs(tp - entry) if tp and entry else 0
        shown_rr = reward / risk if risk > 0 else 0
        realized_r = float(c.get("realized_r") or 0)
        trades.append({
            "pid": pid,
            "symbol": sym,
            "side": o.get("side", ""),
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "shown_rr": round(shown_rr, 2),
            "achieved_rr": realized_r,
            "close_reason": str(c.get("reason", "")),
            "fill_type": o.get("fill_type", ""),
            "open_ts": int(o.get("ts") or 0),
            "bar_id": o.get("bar_id", ""),
        })
    return sorted(trades, key=lambda t: t["open_ts"])


def load_m2_signals(symbol_filter=None) -> list[dict]:
    path = ROOT / "data" / "mode_compare.jsonl"
    out: dict[tuple, dict] = {}
    for line in path.open():
        try:
            r = json.loads(line)
            sym, bar = r.get("symbol", ""), r.get("bar_id", "")
            if not sym or not bar:
                continue
            if symbol_filter and sym != symbol_filter:
                continue
            key = (sym, bar)
            existing = out.get(key)
            if existing and abs(existing["score"]) >= abs(r.get("score", 0)):
                continue
            out[key] = {
                "symbol": sym, "bar_id": bar,
                "side": r.get("side"),
                "score": float(r.get("score") or 0),
                "bias": int(r.get("bias_strength") or 1),
                "votes": r.get("votes", []),
                "ts": int(r.get("ts") or 0),
            }
        except Exception:
            pass
    return sorted(out.values(), key=lambda r: r["ts"])


# ── Stats helpers ──────────────────────────────────────────────────────────────

def _rr_stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "total": 0.0, "avg": 0.0, "win_pct": 0.0, "wins": 0}
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": len(vals),
        "total": round(sum(vals), 2),
        "avg": round(sum(vals) / len(vals), 2),
        "win_pct": round(wins / len(vals) * 100, 1),
        "wins": wins,
    }


def _section(title: str):
    print(f"\n{'═'*66}")
    print(f"  {title}")
    print(f"{'═'*66}")


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--last", type=int, default=30)
    ap.add_argument("--no-fetch", action="store_true", help="use cached OHLC only")
    args = ap.parse_args()

    m1_trades = load_m1_trades(args.symbol)
    m2_signals = load_m2_signals(args.symbol)

    symbols = sorted({t["symbol"] for t in m1_trades}) if not args.symbol else [args.symbol]
    cache_dir = ROOT / "data" / "raw"

    # ── Fetch / load OHLC ─────────────────────────────────────────────────────
    if m2_signals:
        ts_min = min(s["ts"] for s in m2_signals) - 900
        ts_max = max(s["ts"] for s in m2_signals) + 900
    else:
        ts_min = int(time.time()) - 86400
        ts_max = int(time.time())

    print(f"\n  Fetching OHLC: {_ts_ist(ts_min)} → {_ts_ist(ts_max)}")
    ohlc = fetch_ohlc(symbols, ts_min, ts_max, cache_dir, no_fetch=args.no_fetch)

    # Build ts→index lookup per symbol for fast bar lookup
    bar_idx: dict[str, dict[int, int]] = {}
    for sym, bars in ohlc.items():
        bar_idx[sym] = {b["ts"]: i for i, b in enumerate(bars)}

    # ── Mode 1: achieved vs shown RR ──────────────────────────────────────────
    _section("MODE 1 (Claude) — Achieved vs Shown RR")
    for sym in symbols:
        trades = [t for t in m1_trades if t["symbol"] == sym]
        if not trades:
            continue
        shown = [t["shown_rr"] for t in trades]
        ach = [t["achieved_rr"] for t in trades]
        ss = _rr_stats(shown)
        sa = _rr_stats(ach)
        capture = sa["avg"] / ss["avg"] * 100 if ss["avg"] else 0

        print(f"\n  ── {sym} ({len(trades)} closed) ──")
        print(f"  Shown RR:    avg={ss['avg']:+.2f}  total={ss['total']:+.2f}")
        print(f"  Achieved RR: avg={sa['avg']:+.2f}  total={sa['total']:+.2f}  "
              f"win={sa['wins']}/{sa['n']} ({sa['win_pct']}%)")
        print(f"  Capture:     {capture:.0f}% of shown RR realized")

        print()
        print(f"  {'IST':<16} {'side':<6} {'entry':>8} {'SL':>8} {'TP':>8} "
              f"{'shown':>6} {'achv':>6} {'reason':<22}")
        print(f"  {'-'*86}")
        for t in trades[-args.last:]:
            mark = "✓" if t["achieved_rr"] > 0 else "✗"
            print(f"  {_ts_ist(t['open_ts']):<16} {t['side']:<6} "
                  f"{t['entry']:>8.2f} {t['sl']:>8.2f} {t['tp']:>8.2f} "
                  f"{t['shown_rr']:>6.2f} {t['achieved_rr']:>6.2f} "
                  f"{mark} {t['close_reason'][:20]:<20}")

        reason_groups: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            key = ("tp_hit" if "tp_hit" in t["close_reason"] else
                   "sl_hit" if "sl_hit" in t["close_reason"] else
                   t["close_reason"].split(" ")[0])
            reason_groups[key].append(t["achieved_rr"])
        print(f"\n  Close reasons:")
        for reason, rrs in sorted(reason_groups.items(), key=lambda x: -len(x[1])):
            st = _rr_stats(rrs)
            print(f"    {reason:<25} n={st['n']:>3}  avg={st['avg']:>+.2f}  total={st['total']:>+.2f}")

    # ── Mode 2: price-walk-forward simulation ─────────────────────────────────
    _section(f"MODE 2 (Rules) — Price Walk-Forward Simulation  "
             f"[SL={SL_ATR_MULT}×ATR  TP={TP_ATR_MULT}×ATR  shown_RR={SHOWN_RR}]")

    m2_results: list[dict] = []
    for sig in m2_signals:
        sym = sig["symbol"]
        if sym not in ohlc or sig["side"] == "flat":
            m2_results.append({**sig, "sim": None})
            continue

        bars = ohlc[sym]
        idx_map = bar_idx.get(sym, {})

        # Find bar closest to signal time (signal fires ~bar close → look within 90s)
        sig_ts = sig["ts"]
        # 1m bar open times are at 0-second marks; signal fires shortly after bar close
        candidate_ts = (sig_ts // 60) * 60   # round down to minute
        idx = idx_map.get(candidate_ts) or idx_map.get(candidate_ts - 60)
        if idx is None:
            # fallback: find nearest bar within 120s
            nearest = min(
                (abs(b["ts"] - sig_ts), i)
                for i, b in enumerate(bars)
                if abs(b["ts"] - sig_ts) < 120
            ) if bars else None
            idx = nearest[1] if nearest else None

        if idx is None:
            m2_results.append({**sig, "sim": {"outcome": "no_bar", "achieved_rr": None}})
            continue

        sim = simulate_signal(sig["side"], idx, bars)
        m2_results.append({**sig, "sim": sim})

    for sym in symbols:
        sym_sims = [r for r in m2_results if r["symbol"] == sym and r.get("sim")]
        non_flat = [r for r in sym_sims if r["side"] != "flat" and r["sim"]]
        flat_ct = sum(1 for r in sym_sims if r["side"] == "flat")
        no_data = sum(1 for r in non_flat if r["sim"].get("outcome") == "no_bar")
        simulated = [r for r in non_flat if r["sim"].get("achieved_rr") is not None
                     and r["sim"].get("outcome") != "no_bar"]

        ach_rrs = [r["sim"]["achieved_rr"] for r in simulated]
        sa = _rr_stats(ach_rrs)

        # Outcome breakdown
        outcomes: dict[str, list[float]] = defaultdict(list)
        for r in simulated:
            outcomes[r["sim"]["outcome"]].append(r["sim"]["achieved_rr"])

        print(f"\n  ── {sym} ──")
        print(f"  Signals: {len(sym_sims)} total | active={len(non_flat)} flat={flat_ct} | "
              f"simulated={len(simulated)} no_bar={no_data}")
        print(f"  Shown RR (fixed): {SHOWN_RR:+.2f} per trade")
        if sa["n"]:
            capture = sa["avg"] / SHOWN_RR * 100 if SHOWN_RR else 0
            print(f"  Achieved RR: avg={sa['avg']:>+.2f}  total={sa['total']:>+.2f}  "
                  f"win={sa['wins']}/{sa['n']} ({sa['win_pct']}%)")
            print(f"  Capture:     {capture:.0f}% of shown RR")
        for outcome, rrs in sorted(outcomes.items(), key=lambda x: -len(x[1])):
            st = _rr_stats(rrs)
            print(f"    {outcome:<12} n={st['n']:>3}  avg={st['avg']:>+.2f}  total={st['total']:>+.2f}")

        # Per-signal table (last N)
        recent = [r for r in simulated][-args.last:]
        if recent:
            print()
            print(f"  {'IST':<16} {'side':<6} {'entry':>8} {'SL':>8} {'TP':>8} "
                  f"{'shown':>6} {'achv':>6} {'outcome':<10} {'held':>5} {'ATR15m':>8}")
            print(f"  {'-'*94}")
            for r in recent:
                s = r["sim"]
                mark = "✓" if s["achieved_rr"] > 0 else "✗"
                print(f"  {_ts_ist(r['ts']):<16} {r['side']:<6} "
                      f"{s.get('entry', 0):>8.2f} {s.get('sl', 0):>8.2f} {s.get('tp', 0):>8.2f} "
                      f"{SHOWN_RR:>6.2f} {s['achieved_rr']:>6.2f} "
                      f"{mark} {s['outcome']:<10} {s['bars_held']:>5} "
                      f"{s.get('atr_15m', 0):>8.2f}")

    # ── M2 actual paper (if available) ────────────────────────────────────────
    m2_paper_path = ROOT / "data" / "positions_m2.jsonl"
    if m2_paper_path.exists() and m2_paper_path.stat().st_size > 0:
        _section("MODE 2 ACTUAL PAPER OUTCOMES (positions_m2.jsonl)")
        m2_actual = load_m1_trades(args.symbol, path=m2_paper_path)
        for sym in symbols:
            trades = [t for t in m2_actual if t["symbol"] == sym]
            if not trades:
                continue
            shown = [t["shown_rr"] for t in trades]
            ach = [t["achieved_rr"] for t in trades]
            ss = _rr_stats(shown)
            sa = _rr_stats(ach)
            print(f"\n  ── {sym} ({len(trades)} closed) ──")
            print(f"  Shown RR:    avg={ss['avg']:+.2f}  total={ss['total']:+.2f}")
            print(f"  Achieved RR: avg={sa['avg']:+.2f}  total={sa['total']:+.2f}  "
                  f"win={sa['wins']}/{sa['n']} ({sa['win_pct']}%)")
    else:
        print(f"\n  [M2 paper not yet active — restart stack to begin collecting fills]")

    # ── Head-to-head summary ───────────────────────────────────────────────────
    _section("HEAD-TO-HEAD SUMMARY")
    print()
    print(f"  {'Symbol':<12} {'M1 total RR':>12} {'M1 WR':>8} {'M1 shown':>9} "
          f"{'M2 sim total':>13} {'M2 sim WR':>10} {'M2 shown':>9}")
    print(f"  {'-'*78}")
    for sym in symbols:
        t1 = [t for t in m1_trades if t["symbol"] == sym]
        s1 = _rr_stats([t["achieved_rr"] for t in t1])
        s1sh = _rr_stats([t["shown_rr"] for t in t1])
        sym_sim = [r for r in m2_results
                   if r["symbol"] == sym and r.get("sim")
                   and r["sim"].get("achieved_rr") is not None
                   and r["sim"].get("outcome") != "no_bar"]
        s2 = _rr_stats([r["sim"]["achieved_rr"] for r in sym_sim])
        shown_total = round(SHOWN_RR * s2["n"], 2)
        print(f"  {sym:<12} {s1['total']:>+12.2f} {s1['win_pct']:>7.1f}% {s1sh['total']:>+9.2f} "
              f"{s2['total']:>+13.2f} {s2['win_pct']:>9.1f}% {shown_total:>+9.2f}")

    print()
    print(f"  M2 SL={SL_ATR_MULT}×ATR_15m  TP={TP_ATR_MULT}×ATR_15m  walk_limit={WALK_LIMIT} bars")
    print(f"  OHLC source: Bybit (BTCUSDT=linear, XAUTUSDT=spot)")
    print(f"  M2 paper actual fills → data/positions_m2.jsonl (restart to activate)")


if __name__ == "__main__":
    main()
