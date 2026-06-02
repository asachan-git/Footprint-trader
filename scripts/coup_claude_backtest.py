#!/usr/bin/env python3
"""A/B: Claude vs our rules engine on the coup setup (backtest, no live trading).

For each high-volume candidate bar (BTC 15m), feed Claude a short two-zone
footprint window + the coup system prompt (prompts/system/coup.txt) and let it
FULL-decide (setup? side? entry/SL/TP, or flat). On the SAME candidate bars, run
our rules coup (momentum) and coup_reversal (two-zone). Simulate every resulting
trade identically (first-touch SL/TP, time-stop) and report the three side by side.

No-future-leak: Claude/rules see bars only up to the trigger (+1 confirm candle);
entry is the next bar's close, simulation walks strictly forward.

Usage: .venv/bin/python scripts/coup_claude_backtest.py [SYMBOL] [--limit N]
Needs ANTHROPIC_API_KEY (in .env). Candidate calls are bounded by --limit.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from pipeline.state_store import store
from pipeline.footprint import build as build_fp
from llm.client import ClaudeClient, ClientConfig
import scripts.coup_backtest as cb           # reuse _simulate / _patch_store / Coup
from strategies.coup import Coup

SYS_PROMPT = (ROOT / "prompts" / "system" / "coup.txt").read_text()
TF = "15m"
VOL_LOOKBACK = 10
VOL_MULT = 1.8          # candidate floor (shared opportunity set)
WINDOW = 5              # prior bars of context before the trigger
DEFAULT_RR = 2.0


def _zone(bar, lo, hi):
    a = sum(v.vol for v in bar.ask_ladder if lo <= v.price <= hi)
    b = sum(v.vol for v in bar.bid_ladder if lo <= v.price <= hi)
    return round(a, 2), round(b, 2)


def encode_bar(bar):
    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    rng = max(h - l, 1e-9)
    fp = build_fp(bar)
    ez = rng * 0.10
    band = rng * 0.12
    la, lb = _zone(bar, l, l + ez)           # low zone
    ha, hb = _zone(bar, h - ez, h)           # high zone
    nla, nlb = _zone(bar, c - band, c)       # near close (below)
    nha, nhb = _zone(bar, c, c + band)       # near close (above)
    return {
        "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
        "bull": c > o, "vol": round(fp.total_bid + fp.total_ask, 1),
        "delta": round(fp.delta, 1),
        "low_zone": {"ask": la, "bid": lb},
        "high_zone": {"ask": ha, "bid": hb},
        "near_close_below": {"ask": nla, "bid": nlb},
        "near_close_above": {"ask": nha, "bid": nhb},
    }


def candidates(bars):
    out = []
    for i in range(VOL_LOOKBACK + 1, len(bars) - 3):
        prior = [t for t in (Coup._total_vol(b) for b in bars[i - VOL_LOOKBACK:i]) if t > 0]
        if not prior:
            continue
        med = statistics.median(prior)
        if med > 0 and Coup._total_vol(bars[i]) >= VOL_MULT * med:
            out.append(i)
    return out


def sim(side, entry, sl, tp, future):
    res = cb._simulate(side, entry, sl, tp, future)
    return res  # (r, reason, exit_off, exit_price) or None


def run_claude(bars, cand, client):
    trades = []
    for i in cand:
        window = bars[i - WINDOW:i + 2]      # context + trigger + 1 confirm candle
        ctx = {"window": [encode_bar(b) for b in window],
               "note": "last two candles = trigger then confirmation"}
        try:
            d = client.decide(SYS_PROMPT, json.dumps(ctx))
        except Exception as e:
            print(f"  claude err @{i}: {type(e).__name__}: {e}")
            continue
        print(f"  claude @{i}: side={d.side} entry={d.entry} sl={d.stop_loss} "
              f"conf={d.confidence} :: {(d.rationale or '')[:90]}")
        if d.side not in ("long", "short") or d.entry is None:
            continue
        entry = float(bars[i + 1].ohlc.c)    # enter at confirm-candle close
        sl = float(d.stop_loss) if d.stop_loss is not None else (
            bars[i].ohlc.l if d.side == "long" else bars[i].ohlc.h)
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = float(d.take_profit) if d.take_profit is not None else (
            entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk)
        if (d.side == "long" and tp <= entry) or (d.side == "short" and tp >= entry):
            tp = entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk
        r = sim(d.side, entry, sl, tp, bars[i + 2:])
        if r:
            trades.append({"i": i, "side": d.side, "r": r[0], "reason": r[1],
                           "conf": d.confidence})
    return trades


def run_rules(bars, cand, cfg):
    """Run a rules-coup variant; record a trade when it decides on a candidate bar."""
    inst = Coup(config=cfg)
    trades = []
    for i in cand:
        cb._patch_store(bars, i + 1)
        try:
            d = inst.decide(cfg["symbols"][0], TF, bars[i], {})
        except Exception:
            continue
        if not d or d.side not in ("long", "short") or d.entry is None:
            continue
        entry = float(d.entry)
        future = bars[i + 1:]
        em = cfg.get("entry_mode", "close")
        if em != "close":
            fk = cb._try_fill(d.side, entry, future)
            if fk is None:
                continue
            future = future[fk + 1:]
        sl = float(d.stop_loss)
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk
        r = sim(d.side, entry, sl, tp, future)
        if r:
            trades.append({"i": i, "side": d.side, "r": r[0], "reason": r[1]})
    return trades


def summary(name, trades):
    if not trades:
        print(f"  {name:18s} n=0"); return
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    longs = sum(1 for t in trades if t["side"] == "long")
    print(f"  {name:18s} n={len(rs):3d}  WR={100*wins/len(rs):3.0f}%  "
          f"sumR={sum(rs):+.2f}  avgR={statistics.mean(rs):+.3f}  "
          f"long/short={longs}/{len(rs)-longs}")


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "BTCUSDT"
    limit = 60
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    bars = store().recent(sym, TF, 10_000_000)
    cand = candidates(bars)[-limit:]
    print(f"{sym} {TF}: {len(bars)} bars, {len(cand)} candidate triggers (vol≥{VOL_MULT}×med), "
          f"Claude calls={len(cand)}\n")
    if not cand:
        print("no candidates"); return

    client = ClaudeClient(ClientConfig(timeout_s=45.0, max_tokens=1500))
    claude = run_claude(bars, cand, client)
    rules_mom = run_rules(bars, cand, {"symbols": [sym], "absorption_mode": "momentum",
                                       "vol_lookback": VOL_LOOKBACK, "entry_mode": "range",
                                       "sl_mode": "imbalance"})
    rules_rev = run_rules(bars, cand, {"symbols": [sym], "absorption_mode": "reversal",
                                       "vol_lookback": VOL_LOOKBACK, "vol_mult_max": 2.5,
                                       "confirm_delta_ratio": 0.15, "entry_mode": "imbalance",
                                       "sl_mode": "imbalance"})
    cb._patch_store(bars, len(bars))  # restore

    print("=== A/B on the same candidate bars (entry@confirm-close, 2R sim) ===")
    summary("claude", claude)
    summary("rules:coup(mom)", rules_mom)
    summary("rules:coup_reversal", rules_rev)


if __name__ == "__main__":
    main()
