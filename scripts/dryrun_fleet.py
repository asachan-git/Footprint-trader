#!/usr/bin/env python3
"""Info-only fleet dry run — every enabled strategy, ANY symbol/TF, two versions:
  A) baseline             — take every decide() entry
  B) +cvd_div gate        — keep only trades whose side matches the LAST confirmed
                            CVD-divergence direction (on the run's TF) as of the bar

TF-INDEPENDENT: behaviour is identical on any TF — horizons/windows are in BARS and
the div-marker lag is scaled by the TF's bar duration. Pick TF/symbol via CLI.
NOT a deployment test (1m especially is exploratory; the fleet is 15m/5m-calibrated).
Walks closed bars, asks each instance to .decide() at the cutoff (no future
leak via a monkeypatched store.recent + CUT ts), simulates the trade forward
(first-touch SL/TP, SL-first, time-stop at MAX_HOLD), and prints per-instance
A-vs-B stats so we can see where "last CVD div direction only" helps.

Method notes (relative ranking, not live-exact):
  - All instances forced to vote_tf/decide_tf = 1m, symbols = [XAUTUSDT].
  - Vote-family decide() emits a placeholder ±5% SL/TP → replaced with an ATR
    stop (sl_atr_mult×ATR, default 1.5) + 2R target so R math is meaningful.
    Footprint strategies' real SL/TP are kept.
  - The gate is a strict SUBSET filter of baseline trades (same entries, minus
    rejected) — conservative apples-to-apples, does not re-pick entries freed up
    while baseline was in a trade.
  - CVD-div markers precomputed once on full history; gate uses the last marker
    with ts ≤ signal_ts − LAG (LAG = swing-confirm lag, removes pivot lookahead).

    .venv/bin/python -u scripts/dryrun_fleet.py                      # XAU 1m (default)
    .venv/bin/python -u scripts/dryrun_fleet.py --symbol BTCUSDT --tf 15m --window 2000
"""
from __future__ import annotations

import bisect
import logging
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.cvd_candlestick import scan_divergences
from strategies.registry import build_enabled
import strategies.democracy as _demomod

# TF-independent: behaviour is identical on any TF — all horizons/windows are in
# BARS (not wall-clock), and the only time-based quantity (the div-marker lag) is
# scaled by the bar duration of the chosen TF. Set via CLI (--symbol/--tf/--window).
SYMBOL = "XAUTUSDT"
TF = "1m"               # STEP/timing TF — the loop cadence + forward-sim granularity
HTF = None              # STRUCTURE TF (--htf). None → single-TF (structure == step).
                        # When set (e.g. 15m): strategies read structure on HTF, but we
                        # step/enter/sim on TF → "HTF structure, LTF timing".
_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
WINDOW = 6000         # decide only over the last WINDOW bars
WARMUP = 120          # bars of history before first decision (vote needs ~100)
MAX_HOLD = 60         # bars before time-stop (forward horizon, in bars)
FILL_WITHIN = 4       # bars a limit entry has to fill
DEFAULT_RR = 2.0      # TP target when decide() gives a placeholder/None
SL_ATR_MULT = 1.5     # ATR stop when decide() SL is a placeholder
DIV_LAG_BARS = 2      # confirm-lag (swing lookback) before a div marker is "known"

# ── store time-travel: every strategy sees only bars up to CUT["ts"] ──────────
S = store()
_orig = S.recent
CUT = {"ts": None}


def _recent(symbol, tf, n):
    bars = _orig(symbol, tf, 10_000_000)
    if CUT["ts"] is not None:
        bars = [b for b in bars if b.close_ts <= CUT["ts"]]
    return bars[-n:] if n else bars


S.recent = _recent


# ── share the vote across all vote-family instances (identical per bar) ───────
# democracy/republic/senate/congress all funnel through Democracy.decide, which
# calls the heavy decide_direction(symbol, vote_tf) (~1s on 1m). Memoize it per
# (symbol, vote_tf, cutoff_ts) so 6 vote instances cost 1 real vote per bar.
_real_dd = _demomod.decide_direction
_dd_cache: dict = {}


def _cached_dd(symbol, primary_tf="15m"):
    key = (symbol, primary_tf, CUT["ts"])
    v = _dd_cache.get(key)
    if v is None:
        _dd_cache.clear()           # only the current bar is ever reused
        v = _real_dd(symbol, primary_tf)
        _dd_cache[key] = v
    return v


_demomod.decide_direction = _cached_dd


def _is_placeholder_sl(entry, sl):
    """Democracy/vote family emits exactly ±5% SL — detect to replace with ATR stop."""
    if sl is None or entry in (None, 0):
        return True
    return abs(abs(sl - entry) / entry - 0.05) < 1e-4


def _atr15(bars, upto):
    return atr(bars[max(0, upto - 14): upto + 1], period=14)


def _simulate(side, entry, sl, tp, future):
    risk = abs(entry - sl) or 1e-9
    for k, b in enumerate(future):
        if k >= MAX_HOLD:
            r = ((b.ohlc.c - entry) if side == "long" else (entry - b.ohlc.c)) / risk
            return r, "hold", k
        if side == "long" and b.ohlc.l <= sl:  return -1.0, "sl", k
        if side == "short" and b.ohlc.h >= sl: return -1.0, "sl", k
        if side == "long" and b.ohlc.h >= tp:  return abs(tp - entry) / risk, "tp", k
        if side == "short" and b.ohlc.l <= tp: return abs(entry - tp) / risk, "tp", k
    if not future:
        return None
    last = future[-1].ohlc.c
    r = ((last - entry) if side == "long" else (entry - last)) / risk
    return r, "hold", len(future) - 1


def _one_decision(name, strat, bars, i, errors):
    """Run strat.decide at bar i, simulate, return a trade dict + open_until, or None."""
    try:
        d = strat.decide(SYMBOL, TF, bars[i], {})
    except Exception as e:  # noqa: BLE001
        errors[name] = errors.get(name, 0) + 1
        errors[f"{name}:last"] = f"{type(e).__name__}: {e}"
        return None
    if not d or d.side not in ("long", "short") or d.entry is None:
        return None
    future = bars[i + 1:]
    if not future:
        return None
    try:
        entry = float(d.entry)
        # limit fill if entry is away from the signal close, else market-at-close
        close = bars[i].ohlc.c
        is_limit = (d.side == "long" and entry < close) or (d.side == "short" and entry > close)
        if is_limit:
            fk = next((k for k, b in enumerate(future[:FILL_WITHIN])
                       if b.ohlc.l <= entry <= b.ohlc.h), None)
            if fk is None:
                return None
            sim_future = future[fk + 1:]
        else:
            entry = close
            sim_future = future

        sl = float(d.stop_loss) if d.stop_loss is not None else None
        if _is_placeholder_sl(entry, sl):
            # placeholder SL → ATR stop. In split mode use the STRUCTURE-TF ATR (the
            # stop should match the 15m structure the vote reasoned on), not the 1m
            # timing-TF ATR. atr_from_store reads the CUT-time-travelled store → causal.
            _stf = HTF or TF
            if _stf != TF:
                from pipeline.features.atr import atr_from_store
                a = atr_from_store(SYMBOL, _stf, period=14)
            else:
                a = _atr15(bars, i)
            if a <= 0:
                return None
            sl = entry - SL_ATR_MULT * a if d.side == "long" else entry + SL_ATR_MULT * a
        risk = abs(entry - sl)
        if risk <= 0:
            return None

        tp = float(d.take_profit) if d.take_profit is not None else None
        if tp is None or (d.side == "long" and tp <= entry) or (d.side == "short" and tp >= entry) \
                or _is_placeholder_sl(entry, d.stop_loss):
            tp = entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk

        res = _simulate(d.side, entry, sl, tp, sim_future)
        if res is None:
            return None
        r, reason, off = res
    except Exception as e:  # noqa: BLE001
        errors[name] = errors.get(name, 0) + 1
        errors[f"{name}:last"] = f"sim {type(e).__name__}: {e}"
        return None
    base_idx = i if not is_limit else i + 1
    open_until = min(base_idx + 1 + off, len(bars) - 1)
    trade = {"side": d.side, "signal_ts": bars[i].close_ts, "r": float(r), "reason": reason}
    return trade, open_until


def _prime_features(bars, i):
    """Populate the per-bar feature state the LIVE pipeline maintains, so
    registry-backed strategies can fire in the dry run (else they always return
    None → '--'). Specifically the sweep registry, which reversal_eqhl gates on via
    active_sweeps(). Causal: detect+tick only on bars ≤ i (loop advances i
    monotonically, mirroring live ingest)."""
    try:
        from pipeline.features.swing import build as _swing_build
        from pipeline.features.sweep import detect as _sweep_detect, tick_registry as _tick
        win = bars[max(0, i - 200):i + 1]
        sp = _swing_build(SYMBOL, TF, win)
        _sweep_detect(bars[i], sp, win[:-1])
        _tick(SYMBOL, bars[i])
    except Exception:
        pass


def replay_all(instances, step_bars, struct_bars, errors):
    """Step over STEP-TF bars (timing/forward-sim); strategies read STRUCTURE on the
    struct-TF bars (== step_bars in single-TF mode). The sweep registry is primed on
    STRUCTURE bars. In split mode each instance enters at most once per struct bar (a
    single HTF signal → one LTF-timed entry, not one per LTF bar). One open trade at a
    time per instance. Returns {name: [trades]}."""
    from pipeline.features.sweep import reset_registry
    reset_registry(SYMBOL)
    state = {inst.name: {"open_until": -1, "trades": [], "last_struct_ts": None}
             for inst in instances}
    start = max(WARMUP, len(step_bars) - WINDOW)
    sj = 0   # struct-bar priming cursor (advances as the step cutoff crosses struct closes)
    for i in range(start, len(step_bars) - 2):
        CUT["ts"] = step_bars[i].close_ts
        while sj < len(struct_bars) and struct_bars[sj].close_ts <= CUT["ts"]:
            _prime_features(struct_bars, sj)     # registry on STRUCTURE bars, causal
            sj += 1
        cur_struct_ts = struct_bars[sj - 1].close_ts if sj > 0 else None
        if cur_struct_ts is None:
            continue
        for inst in instances:
            st = state[inst.name]
            if i <= st["open_until"]:
                continue
            if st["last_struct_ts"] == cur_struct_ts:
                continue                         # one decide()/entry per struct bar
            st["last_struct_ts"] = cur_struct_ts # mark attempted (structure is fixed
                                                 # within a struct bar → decide is stable;
                                                 # avoids paying the vote 15× per HTF bar)
            out = _one_decision(inst.name, inst, step_bars, i, errors)
            if out is None:
                continue
            trade, open_until = out
            st["trades"].append(trade)
            st["open_until"] = open_until
    return {name: st["trades"] for name, st in state.items()}


def _summ(trades):
    rs = [t["r"] for t in trades]
    if not rs:
        return None
    wins = [r for r in rs if r > 0]; losses = [r for r in rs if r <= 0]
    gw = sum(wins); gl = abs(sum(losses))
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return {"n": len(rs), "win": 100 * len(wins) / len(rs), "sumR": sum(rs),
            "avgR": statistics.mean(rs), "pf": pf}


def _fmt(s):
    if s is None:
        return f"{'--':>4s} {'':>5s} {'':>8s} {'':>7s} {'':>6s}"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"{s['n']:>4d} {s['win']:>4.0f}% {s['sumR']:>8.2f} {s['avgR']:>7.3f} {pf:>6s}"


def _report_path():
    suffix = f"_{TF}" + (f"_struct{HTF}" if HTF and HTF != TF else "")
    return ROOT / "data" / "reports" / f"dryrun_{SYMBOL}{suffix}.md"
_LINES: list[str] = []


def P(s: str = "") -> None:
    """Print AND capture, so the run is persisted to REPORT (stdout-only output
    was lost between sessions)."""
    print(s)
    _LINES.append(str(s))


def main():
    struct_tf = HTF or TF                       # structure TF (where strategies "think")
    split = struct_tf != TF
    step_bars = _orig(SYMBOL, TF, 10_000_000)   # timing/forward-sim cadence
    struct_bars = _orig(SYMBOL, struct_tf, 10_000_000) if split else step_bars
    decided = min(WINDOW, max(0, len(step_bars) - WARMUP))
    ctx = f"structure={struct_tf} / timing={TF}" if split else TF
    P(f"=== {SYMBOL} {ctx} DRY RUN (info only) — {len(step_bars)} {TF} bars, "
          f"deciding over last {decided}, horizon={MAX_HOLD} bars ===\n")

    # CVD-div markers + gate on the STRUCTURE TF (the context the gate reasons about)
    CUT["ts"] = None
    markers = scan_divergences(struct_bars, lookback=3, include_live=False)
    mk = sorted((m["ts"], 1 if m["type"] == "bull" else -1) for m in markers)
    mk_ts = [t for t, _ in mk]
    lag = DIV_LAG_BARS * _TF_SEC.get(struct_tf, 60)
    P(f"CVD-div markers on {struct_tf}: {len(mk)} "
          f"({sum(1 for _,d in mk if d>0)} bull / {sum(1 for _,d in mk if d<0)} bear)\n")

    def last_div_dir(signal_ts):
        idx = bisect.bisect_right(mk_ts, signal_ts - lag) - 1
        return mk[idx][1] if idx >= 0 else 0

    # instances evaluate STRUCTURE on struct_tf (HTF context preserved when split)
    instances = build_enabled()
    for inst in instances:
        inst.config["vote_tf"] = struct_tf
        inst.config["decide_tf"] = struct_tf
        inst.config["symbols"] = [SYMBOL]

    errors = {}
    CUT["ts"] = None
    base_by_name = replay_all(instances, step_bars, struct_bars, errors)
    rows = []
    for inst in instances:
        base = base_by_name.get(inst.name, [])
        gated = [t for t in base if t["side"] == ("long" if last_div_dir(t["signal_ts"]) > 0
                                                  else "short" if last_div_dir(t["signal_ts"]) < 0 else None)]
        rows.append((inst.name, _summ(base), _summ(gated)))

    CUT["ts"] = None
    hdr = (f"{'strategy':22s} | {'A: baseline':^32s} | {'B: +cvd_div gate':^32s}")
    sub = (f"{'':22s} | {'n':>4s} {'win':>5s} {'sumR':>8s} {'avgR':>7s} {'PF':>6s} "
           f"| {'n':>4s} {'win':>5s} {'sumR':>8s} {'avgR':>7s} {'PF':>6s}")
    P(hdr); P(sub); P("-" * len(sub))
    for name, a, b in rows:
        P(f"{name:22s} | {_fmt(a)} | {_fmt(b)}")

    # fleet totals
    P("-" * len(sub))
    base_all = [(name, a) for name, a, _ in rows if a]
    gate_all = [(name, b) for name, _, b in rows if b]
    ta = {"n": sum(a["n"] for _, a in base_all), "sumR": sum(a["sumR"] for _, a in base_all)}
    tb = {"n": sum(b["n"] for _, b in gate_all), "sumR": sum(b["sumR"] for _, b in gate_all)}
    P(f"{'FLEET':22s} | n={ta['n']:>4d} sumR={ta['sumR']:>8.2f}{'':>16s} "
          f"| n={tb['n']:>4d} sumR={tb['sumR']:>8.2f}")

    if errors:
        P("\n=== errors (skipped) ===")
        for k, v in errors.items():
            if k.endswith(":last"):
                P(f"  {k}: {v}")
            else:
                P(f"  {k}: {v} skipped")

    # persist (stdout-only output was lost between sessions)
    try:
        report = _report_path()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("```\n" + "\n".join(_LINES) + "\n```\n")
        print(f"\n[written] {report}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not write report: {e}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fleet dry run — baseline vs +CVD-div gate, any TF/symbol.")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--tf", default=TF, choices=sorted(_TF_SEC), help="STEP/timing TF")
    ap.add_argument("--htf", default=None, choices=sorted(_TF_SEC),
                    help="STRUCTURE TF — strategies read structure here, timing on --tf "
                         "(HTF context + LTF timing). Omit for single-TF.")
    ap.add_argument("--window", type=int, default=WINDOW, help="decision window in BARS")
    # legacy positional: `dryrun ... 600` still sets the window
    ap.add_argument("window_pos", nargs="?", type=int, default=None)
    a = ap.parse_args()
    SYMBOL, TF, HTF = a.symbol, a.tf, a.htf
    WINDOW = a.window_pos if a.window_pos is not None else a.window
    main()
