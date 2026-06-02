#!/usr/bin/env python3
"""Absorption observation logger — what actually happened at each absorption candle.

Scans BTC + XAU historical footprint and, for every candle that qualifies as a
coup (momentum) or coup_reversal (reversal) absorption trigger, records a
structured *observation*: who got trapped, who was the aggressor at the extreme,
what the candle close said, and whether the NEXT candle confirmed the reversal /
continuation. Then groups the observations into sections and prints (+ writes) a
report so we can eyeball which structural patterns actually separate winners.

Reuses the live detector (pipeline.features.absorption.detect_canonical_absorption)
and the strategy's own confirm gate (strategies.coup.Coup._confirm) so the logged
candles are exactly the ones coup / coup_reversal would fire on.

    python scripts/absorption_observations.py                  # BTC+XAU, 15m, both modes
    python scripts/absorption_observations.py --tf 5m
    python scripts/absorption_observations.py --symbols BTCUSDT --mode reversal

Outputs:
    data/observations/absorption_obs.jsonl   (one JSON row per candle)
    data/reports/absorption_observations.md  (sectioned human report)

NOTE: store() globs every *.jsonl in data/footprint and merges main + *_agg files
(both tagged tf=15m) → duplicate close_ts. We load the files directly and dedupe
by close_ts instead, so the scan window is clean.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from pipeline.state_store import _deserialize
from pipeline.footprint import build as build_fp
from pipeline.features.absorption import detect_canonical_absorption
from pipeline.features.atr import atr
from strategies.coup import Coup

FP_DIR = ROOT / "data" / "footprint"
OBS_OUT = ROOT / "data" / "observations" / "absorption_obs.jsonl"
REPORT_OUT = ROOT / "data" / "reports" / "absorption_observations.md"

# gating mirrors config/strategies.yaml (coup + coup_reversal)
PARAMS = {
    "momentum": {"vol_mult": 1.8, "delta_ratio": 0.35, "vol_lb": 20, "confirm_within": 3,
                 "tag": "coup"},
    "reversal": {"vol_mult": 1.8, "delta_ratio": 0.35, "vol_lb": 10, "confirm_within": 2,
                 "tag": "coup_reversal"},
}
CONT_N = 6          # forward window for the continuation label
CONT_ATR = 1.0      # winner-dir move >= this x ATR within CONT_N = continuation


# ── data load ─────────────────────────────────────────────────────────────────
def load_bars(symbol: str, tf: str) -> list:
    """Load + dedupe (by close_ts) all footprint files for symbol/tf."""
    seen: dict[int, object] = {}
    for f in sorted(FP_DIR.glob(f"{symbol}_{tf}*.jsonl")):
        if ".bak" in f.name:
            continue
        for line in f.open():
            if not line.strip():
                continue
            try:
                b = _deserialize(line)
            except Exception:
                continue
            if b.symbol == symbol and b.tf == tf:
                seen[b.close_ts] = b   # later files win on dup ts
    return [seen[k] for k in sorted(seen)]


# ── per-candle structural read ──────────────────────────────────────────────────
def _extreme_split(bar, winner: str) -> dict:
    """Aggressive buy(ask)/sell(bid) volume in the absorbed extreme 10% zone."""
    h, l = bar.ohlc.h, bar.ohlc.l
    rng = max(h - l, 1e-9)
    zone = rng * 0.10
    if winner == "short":            # buyers trapped at the top
        thr = h - zone
        ask = sum(v.vol for v in bar.ask_ladder if v.price >= thr)   # aggressive buys
        bid = sum(v.vol for v in bar.bid_ladder if v.price >= thr)   # aggressive sells
        wick = (h - max(bar.ohlc.o, bar.ohlc.c)) / rng
    else:                            # sellers trapped at the low
        thr = l + zone
        ask = sum(v.vol for v in bar.ask_ladder if v.price <= thr)
        bid = sum(v.vol for v in bar.bid_ladder if v.price <= thr)
        wick = (min(bar.ohlc.o, bar.ohlc.c) - l) / rng
    tot = ask + bid
    return {
        "zone_ask": round(ask, 2), "zone_bid": round(bid, 2),
        "zone_aggressor": ("buy" if ask >= bid else "sell"),
        "zone_buy_frac": round(ask / tot, 3) if tot > 0 else None,
        "extreme_wick_frac": round(wick, 3),
    }


def _next_candle_read(bar, nxt, winner: str) -> dict:
    """Did the immediately following candle confirm the winner direction?"""
    long = winner == "long"
    tot = _tot_vol(nxt)
    d = nxt.delta or 0.0
    dr = abs(d) / max(tot, 1e-9)
    delta_with = (d > 0) if long else (d < 0)
    closed_with = (nxt.ohlc.c > nxt.ohlc.o) if long else (nxt.ohlc.c < nxt.ohlc.o)
    progressed = (nxt.ohlc.c > bar.ohlc.c) if long else (nxt.ohlc.c < bar.ohlc.c)
    # reversal CONFIRM = next bar pushes the winner way with delta + closes past trigger
    confirmed = bool(delta_with and dr >= 0.25 and progressed)
    return {
        "next_dir": "bull" if nxt.ohlc.c > nxt.ohlc.o else "bear",
        "next_delta": round(d, 2), "next_delta_ratio": round(dr, 3),
        "next_delta_with_winner": delta_with,
        "next_closed_with_winner": closed_with,
        "next_progressed_past_trigger": progressed,
        "next_candle_confirms": confirmed,
    }


def _tot_vol(bar) -> float:
    fp = build_fp(bar)
    return fp.total_bid + fp.total_ask


# ── scan ─────────────────────────────────────────────────────────────────────
def scan(symbol: str, tf: str, mode: str) -> list[dict]:
    p = PARAMS[mode]
    bars = load_bars(symbol, tf)
    if len(bars) < 40:
        return []
    inst = Coup(config={"symbols": [symbol], "absorption_mode": mode,
                        "vol_lookback": p["vol_lb"], "confirm_within": p["confirm_within"]})
    gate_delta = mode != "reversal"
    obs: list[dict] = []
    for i in range(p["vol_lb"], len(bars) - 1):
        b = bars[i]
        fp = build_fp(b)
        total = fp.total_bid + fp.total_ask
        prev = [t for t in (_tot_vol(x) for x in bars[i - p["vol_lb"]:i]) if t > 0]
        med = statistics.median(prev) if prev else 0.0
        if med <= 0 or total < p["vol_mult"] * med:
            continue
        if gate_delta and (b.delta is None
                           or abs(b.delta) / max(total, 1e-9) < p["delta_ratio"]):
            continue
        absorbs = detect_canonical_absorption(b, fp, mode=mode)
        if not absorbs:
            continue
        a = absorbs[-1]
        winner = "short" if a.side == "sell" else "long"
        trapped = "buyers@top" if winner == "short" else "sellers@low"
        bar_dir = "bull" if b.ohlc.c > b.ohlc.o else ("bear" if b.ohlc.c < b.ohlc.o else "doji")
        rng = max(b.ohlc.h - b.ohlc.l, 1e-9)
        body = abs(b.ohlc.c - b.ohlc.o) / rng

        split = _extreme_split(b, winner)
        nxt = bars[i + 1]
        nread = _next_candle_read(b, nxt, winner)

        # strategy confirm over its real window
        post = bars[i + 1:i + 1 + p["confirm_within"]]
        try:
            strat_confirm = bool(post) and inst._confirm(winner, b, post, bars[:i + 1 + len(post)])
        except Exception:
            strat_confirm = False

        # forward outcome label (continuation MFE/MAE in ATR)
        atr_v = atr(bars[:i + 1]) or 0.0
        fwd = bars[i + 1:i + 1 + CONT_N]
        c0 = b.ohlc.c
        if fwd:
            if winner == "long":
                mfe = max(x.ohlc.h for x in fwd) - c0
                mae = c0 - min(x.ohlc.l for x in fwd)
            else:
                mfe = c0 - min(x.ohlc.l for x in fwd)
                mae = max(x.ohlc.h for x in fwd) - c0
            cont = atr_v > 0 and mfe >= CONT_ATR * atr_v
        else:
            mfe = mae = 0.0
            cont = False

        obs.append({
            "symbol": symbol, "tf": tf, "mode": mode, "strategy": p["tag"],
            "trigger_ts": b.close_ts, "ist": _ist(b.close_ts, symbol),
            "bar_dir": bar_dir, "body_frac": round(body, 3),
            "ohlc": [round(b.ohlc.o, 2), round(b.ohlc.h, 2), round(b.ohlc.l, 2), round(b.ohlc.c, 2)],
            "winner": winner, "trapped": trapped,
            "trigger_delta": round(b.delta or 0, 2),
            "vol_ratio": round(total / med, 2),
            "delta_ratio": round(abs(b.delta or 0) / max(total, 1e-9), 3),
            "absorption_bar_pct": round(a.bar_pct, 3), "is_wick_trap": a.is_wick_trap,
            **split, **nread,
            "strat_confirm": strat_confirm,
            "fwd_mfe_atr": round(mfe / atr_v, 2) if atr_v > 0 else None,
            "fwd_mae_atr": round(mae / atr_v, 2) if atr_v > 0 else None,
            "continuation": bool(cont),
        })
    return obs


def _ist(ts: int, symbol: str) -> str:
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=5, minutes=30)))
    return dt.strftime("%Y-%m-%d %H:%M IST")


# ── report ───────────────────────────────────────────────────────────────────
def _agg(rows: list[dict]) -> str:
    if not rows:
        return "n=0"
    n = len(rows)
    nxt = sum(1 for r in rows if r["next_candle_confirms"])
    cont = sum(1 for r in rows if r["continuation"])
    mfe = [r["fwd_mfe_atr"] for r in rows if r["fwd_mfe_atr"] is not None]
    mae = [r["fwd_mae_atr"] for r in rows if r["fwd_mae_atr"] is not None]
    return (f"n={n:>3d}  next-confirm={100*nxt/n:>4.0f}%  cont={100*cont/n:>4.0f}%  "
            f"avgMFE={statistics.mean(mfe):>4.2f}atr  avgMAE={statistics.mean(mae):>4.2f}atr"
            if mfe else f"n={n:>3d}  next-confirm={100*nxt/n:>4.0f}%  cont={100*cont/n:>4.0f}%")


def _section(out: list[str], title: str, rows: list[dict], splits: list[tuple]):
    out.append(f"\n### {title}")
    out.append(f"- ALL — {_agg(rows)}")
    for label, pred in splits:
        sub = [r for r in rows if pred(r)]
        out.append(f"- {label} — {_agg(sub)}")


def build_report(allobs: list[dict]) -> str:
    out: list[str] = ["# Absorption Observations",
                      f"\nTotal candles logged: **{len(allobs)}**  "
                      f"(continuation = winner-dir move >= {CONT_ATR}xATR within {CONT_N} bars)\n",
                      "## Overview (count by symbol x strategy)"]
    syms = sorted({r["symbol"] for r in allobs})
    modes = ["momentum", "reversal"]
    out.append("\n| symbol | coup (momentum) | coup_reversal |")
    out.append("|---|---|---|")
    for s in syms:
        c = sum(1 for r in allobs if r["symbol"] == s and r["mode"] == "momentum")
        cr = sum(1 for r in allobs if r["symbol"] == s and r["mode"] == "reversal")
        out.append(f"| {s} | {c} | {cr} |")

    for s in syms:
        for m in modes:
            rows = [r for r in allobs if r["symbol"] == s and r["mode"] == m]
            if not rows:
                continue
            out.append(f"\n## {s} — {PARAMS[m]['tag']} ({m})")
            _section(out, "by who got trapped", rows, [
                ("buyers trapped @top -> SHORT", lambda r: r["winner"] == "short"),
                ("sellers trapped @low -> LONG", lambda r: r["winner"] == "long"),
            ])
            _section(out, "by next-candle confirmation", rows, [
                ("next candle CONFIRMS", lambda r: r["next_candle_confirms"]),
                ("next candle does NOT", lambda r: not r["next_candle_confirms"]),
            ])
            _section(out, "by strategy confirm gate", rows, [
                ("strat_confirm=True", lambda r: r["strat_confirm"]),
                ("strat_confirm=False", lambda r: not r["strat_confirm"]),
            ])
            _section(out, "by extreme aggressor", rows, [
                ("aggressor=buy", lambda r: r["zone_aggressor"] == "buy"),
                ("aggressor=sell", lambda r: r["zone_aggressor"] == "sell"),
            ])
            _section(out, "by vol_ratio", rows, [
                ("< 2.0", lambda r: r["vol_ratio"] < 2.0),
                ("2.0 - 3.0", lambda r: 2.0 <= r["vol_ratio"] < 3.0),
                (">= 3.0", lambda r: r["vol_ratio"] >= 3.0),
            ])
            _section(out, "by wick trap flag", rows, [
                ("is_wick_trap=True", lambda r: r["is_wick_trap"]),
                ("is_wick_trap=False", lambda r: not r["is_wick_trap"]),
            ])
            # notable examples — top 8 by vol_ratio
            ex = sorted(rows, key=lambda r: r["vol_ratio"], reverse=True)[:8]
            out.append("\n**Notable candles (top vol_ratio):**")
            for r in ex:
                res = "CONT" if r["continuation"] else "fail"
                nc = "next=Y" if r["next_candle_confirms"] else "next=N"
                out.append(
                    f"- {r['ist']} {r['bar_dir']} volx{r['vol_ratio']} — {r['trapped']} trapped -> "
                    f"{r['winner'].upper()}; extreme aggressor={r['zone_aggressor']} "
                    f"(buy_frac={r['zone_buy_frac']}); Δ={r['trigger_delta']}; {nc} "
                    f"(next Δ={r['next_delta']}); MFE={r['fwd_mfe_atr']} MAE={r['fwd_mae_atr']} [{res}]")
    return "\n".join(out) + "\n"


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]):
    symbols = ["BTCUSDT", "XAUTUSDT"]
    tf = "15m"
    modes = ["momentum", "reversal"]
    if "--symbols" in argv:
        symbols = argv[argv.index("--symbols") + 1].split(",")
    if "--tf" in argv:
        tf = argv[argv.index("--tf") + 1]
    if "--mode" in argv:
        modes = [argv[argv.index("--mode") + 1]]

    allobs: list[dict] = []
    for s in symbols:
        for m in modes:
            rows = scan(s, tf, m)
            allobs.extend(rows)
            print(f"  {s} {tf} {PARAMS[m]['tag']:14s}: {len(rows)} absorption candles")

    OBS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with OBS_OUT.open("w") as fh:
        for r in allobs:
            fh.write(json.dumps(r) + "\n")
    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(build_report(allobs))
    print(f"\nlogged {len(allobs)} observations → {OBS_OUT}")
    print(f"report → {REPORT_OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
