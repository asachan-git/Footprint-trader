#!/usr/bin/env python3
"""Fleet decision dataset — one labeled row per closed position, ML-ready.

Joins, for EVERY strategy under data/strategies/<name>/:
  decisions.jsonl  (position_id → bias_strength, side, rationale, tp_source)
  positions.jsonl  (open/close events → entry, side, realized_r, reason, ts)

For each closed position it computes:
  OUTCOME    win/loss, realized_r, exit_reason, duration_min,
             mfe_r / mae_r (1m price-walk, disaster-floor denominator — the
             stable frame; placed SL trails so |entry-SL| is unreliable),
             capture = realized_r / mfe_r
  CONDITIONS (all CAUSAL — computed from footprint bars at/<= the decision bar):
             regime (trend_up|trend_down|range via REGIME_N/TREND_T, matches
             direction_engine), slope_atr, atr_pct (volatility), session,
             utc_hour, entry_delta sign, cvd_div (last type + aligned?),
             range_pos (where in the trailing-20 range we entered), with_trend

Outputs:
  data/reports/decision_dataset.csv     (the training table)
  stdout summary: W/L broken down by strategy / regime / session / exit / bias /
                  MFE bucket — the "does each slice have edge" view.

    PYTHONPATH=. .venv/bin/python -u scripts/decision_dataset.py
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRAT_DIR = ROOT / "data" / "strategies"
FP_DIR = ROOT / "data" / "footprint"
OUT_CSV = ROOT / "data" / "reports" / "decision_dataset.csv"

# Regime — mirror execution/direction_engine.py exactly.
REGIME_N = 20
TREND_T = 2.0
# Disaster floor per symbol — mirror grid_placer/cycle_manager/mae_mfe_backfill.
FLOOR_PCT = {"BTCUSDT": 0.03, "XAUTUSDT": 0.015}
DEFAULT_FLOOR_PCT = 0.02

from pipeline.state_store import _deserialize
from pipeline.features.atr import atr as _atr
from pipeline.features.session import current_session
from pipeline.features.cvd_candlestick import scan_divergences


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


# ── per-symbol footprint caches ──────────────────────────────────────────────
_bars15: dict[str, list] = {}        # symbol → [Bar] (deserialized, sorted)
_bars1m: dict[str, list[dict]] = {}  # symbol → [raw dict] (sorted)


def bars15(symbol: str) -> list:
    if symbol not in _bars15:
        raw = _load_jsonl(FP_DIR / f"{symbol}_15m.jsonl")
        bars = [_deserialize(json.dumps(d)) for d in raw]
        bars.sort(key=lambda b: b.close_ts)
        _bars15[symbol] = bars
    return _bars15[symbol]


def bars1m(symbol: str) -> list[dict]:
    if symbol not in _bars1m:
        raw = _load_jsonl(FP_DIR / f"{symbol}_1m.jsonl")
        raw.sort(key=lambda b: b["close_ts"])
        _bars1m[symbol] = raw
    return _bars1m[symbol]


def disaster_risk(symbol: str, entry: float) -> float:
    return entry * FLOOR_PCT.get(symbol, DEFAULT_FLOOR_PCT)


# ── join: closed positions enriched with the decision row ────────────────────
def closed_positions(strategy: str) -> list[dict]:
    sdir = STRAT_DIR / strategy
    decs = {d.get("position_id"): d for d in _load_jsonl(sdir / "decisions.jsonl")
            if d.get("position_id")}
    pos: dict[str, dict] = {}
    for e in _load_jsonl(sdir / "positions.jsonl"):
        pid = e.get("position_id")
        t = e.get("type")
        if not pid:
            continue
        if t == "open":
            pos[pid] = {
                "position_id": pid, "symbol": e.get("symbol"),
                "tf": e.get("tf", "15m"), "side": e.get("side"),
                "entry": e.get("entry"), "sl": e.get("stop_loss"),
                "tp": e.get("take_profit"), "bar_id": e.get("bar_id"),
                "opened_ts": e.get("ts"), "closed_ts": None,
                "reason": None, "realized_r": None, "status": "open",
            }
        elif t == "close" and pid in pos:
            pos[pid].update(status="closed", closed_ts=e.get("ts"),
                            reason=e.get("reason"), realized_r=e.get("realized_r"))
    out = []
    for pid, p in pos.items():
        if p["status"] != "closed" or p["realized_r"] is None:
            continue
        if p["entry"] is None or p["side"] not in ("long", "short"):
            continue
        d = decs.get(pid, {})
        p["bias_strength"] = d.get("bias_strength")
        p["tp_source"] = (d.get("fill") or {}).get("tp_source")
        p["strategy"] = strategy
        out.append(p)
    return out


# ── causal market-condition features at the decision bar ─────────────────────
def _bar_close_ts(p: dict) -> int:
    """Decision bar close_ts: parse 'SYM|tf|ts' bar_id, else floor opened_ts."""
    bid = p.get("bar_id") or ""
    parts = bid.split("|")
    if len(parts) == 3 and parts[2].isdigit():
        return int(parts[2])
    return int(p.get("opened_ts") or 0)


def conditions(p: dict) -> dict:
    sym, side = p["symbol"], p["side"]
    cut = _bar_close_ts(p)
    win = [b for b in bars15(sym) if b.close_ts <= cut]
    f = {"regime": "range", "slope_atr": 0.0, "atr_pct": None,
         "session": "", "utc_hour": None, "entry_delta_sign": 0,
         "cvd_div_last": "", "cvd_div_aligned": None,
         "range_pos": None, "with_trend": None}
    if cut:
        f["session"] = current_session(cut, sym).session
        f["utc_hour"] = datetime.fromtimestamp(cut, tz=timezone.utc).hour
    if len(win) >= REGIME_N + 1:
        atr_val = _atr(win[-REGIME_N:]) or 0.0
        last_c = win[-1].ohlc.c
        if atr_val > 0:
            slope = (last_c - win[-(REGIME_N + 1)].ohlc.c) / atr_val
            f["slope_atr"] = round(slope, 2)
            f["atr_pct"] = round(atr_val / last_c, 5) if last_c else None
            if slope >= TREND_T:
                f["regime"] = "trend_up"
            elif slope <= -TREND_T:
                f["regime"] = "trend_down"
        # where in the trailing-20 range did we enter
        hi = max(b.ohlc.h for b in win[-REGIME_N:])
        lo = min(b.ohlc.l for b in win[-REGIME_N:])
        if hi > lo:
            f["range_pos"] = round((p["entry"] - lo) / (hi - lo), 3)
    f["with_trend"] = ((f["regime"] == "trend_up" and side == "long") or
                       (f["regime"] == "trend_down" and side == "short")) \
        if f["regime"] != "range" else None
    if win:
        d = win[-1].delta
        f["entry_delta_sign"] = (1 if d > 0 else -1 if d < 0 else 0) if d is not None else 0
    # most-recent CVD-div in the causal window
    try:
        divs = scan_divergences(win[-60:], lookback=3, include_live=True) if len(win) >= 10 else []
        if divs:
            last = max(divs, key=lambda dd: dd["ts"])
            f["cvd_div_last"] = last["type"]
            f["cvd_div_aligned"] = (last["type"] == "bull") == (side == "long")
    except Exception:
        pass
    return f


def mfe_mae(p: dict) -> tuple[float | None, float | None, int]:
    sym, side, entry = p["symbol"], p["side"], p["entry"]
    o_ts, c_ts = p["opened_ts"], p["closed_ts"]
    risk = disaster_risk(sym, entry)
    if risk <= 0 or not o_ts or not c_ts:
        return None, None, 0
    win = [b for b in bars1m(sym) if o_ts <= b["close_ts"] <= c_ts + 60]
    if not win:
        return None, None, 0
    max_h = max(b["ohlc"]["h"] for b in win)
    min_l = min(b["ohlc"]["l"] for b in win)
    if side == "long":
        return (max_h - entry) / risk, (min_l - entry) / risk, len(win)
    return (entry - min_l) / risk, (entry - max_h) / risk, len(win)


# ── outcome exit-reason simplification ───────────────────────────────────────
def exit_cat(reason: str | None) -> str:
    r = (reason or "").lower()
    if "hard_sl" in r:
        return "hard_sl"
    if "cycle_tp" in r:
        return "cycle_tp"
    if "cvd_divergence" in r:
        return "cvd_div_exit"
    if "absorption_flip" in r:
        return "absorption_flip"
    if "delta_divergence" in r:
        return "delta_div_exit"
    if "trend-escape" in r or "disaster" in r:
        return "trend_escape"
    return "other"


def build() -> list[dict]:
    strategies = sorted(d.name for d in STRAT_DIR.iterdir()
                        if d.is_dir() and (d / "positions.jsonl").exists())
    rows = []
    for strat in strategies:
        for p in closed_positions(strat):
            mfe, mae, nbars = mfe_mae(p)
            cond = conditions(p)
            r = p["realized_r"]
            rows.append({
                "strategy": p["strategy"], "symbol": p["symbol"], "tf": p["tf"],
                "side": p["side"], "bias_strength": p["bias_strength"],
                "opened_ts": p["opened_ts"], "closed_ts": p["closed_ts"],
                "duration_min": round((p["closed_ts"] - p["opened_ts"]) / 60, 1)
                if p["closed_ts"] and p["opened_ts"] else None,
                "entry": p["entry"], "tp_source": p["tp_source"],
                "exit_reason": exit_cat(p["reason"]),
                "realized_r": round(r, 4), "win": int(r > 0),
                "mfe_r": round(mfe, 3) if mfe is not None else None,
                "mae_r": round(mae, 3) if mae is not None else None,
                "capture": round(r / mfe, 3) if (mfe and mfe > 0) else None,
                "mfe_bars": nbars,
                **cond,
            })
    return rows


# ── reporting ────────────────────────────────────────────────────────────────
def _agg(rows: list[dict]) -> str:
    if not rows:
        return "n=0"
    rs = [x["realized_r"] for x in rows]
    w = [r for r in rs if r > 0]
    l = [r for r in rs if r <= 0]
    pf = (sum(w) / -sum(l)) if (l and sum(l) < 0) else float("inf")
    mfes = [x["mfe_r"] for x in rows if x["mfe_r"] is not None]
    maes = [x["mae_r"] for x in rows if x["mae_r"] is not None]
    return (f"n={len(rs):<4} WR={100*len(w)/len(rs):>3.0f}% "
            f"sumR={sum(rs):>7.1f} avgR={statistics.mean(rs):>6.2f} PF={pf:>5.2f} "
            f"avgMFE={statistics.mean(mfes):>5.2f} avgMAE={statistics.mean(maes):>6.2f}"
            if mfes else
            f"n={len(rs):<4} WR={100*len(w)/len(rs):>3.0f}% "
            f"sumR={sum(rs):>7.1f} avgR={statistics.mean(rs):>6.2f} PF={pf:>5.2f}")


def _group(rows, key):
    g = defaultdict(list)
    for x in rows:
        g[x.get(key)].append(x)
    return g


def report(rows: list[dict]) -> None:
    print("=" * 92)
    print(f"DECISION DATASET — {len(rows)} closed positions across "
          f"{len({r['strategy'] for r in rows})} strategies")
    print("=" * 92)
    print(f"ALL  {_agg(rows)}\n")

    def section(title, key, sort_by_n=True):
        print(f"── by {title} " + "─" * (80 - len(title)))
        g = _group(rows, key)
        items = sorted(g.items(), key=(lambda kv: -len(kv[1])) if sort_by_n
                       else (lambda kv: str(kv[0])))
        for k, rs in items:
            print(f"  {str(k):<16} {_agg(rs)}")
        print()

    section("strategy", "strategy")
    section("regime", "regime", sort_by_n=False)
    section("with_trend", "with_trend", sort_by_n=False)
    section("session", "session")
    section("exit_reason", "exit_reason")
    section("bias_strength", "bias_strength", sort_by_n=False)
    section("cvd_div_aligned", "cvd_div_aligned", sort_by_n=False)

    # MFE bucket — entry quality independent of exits
    print("── by MFE bucket (entry quality) " + "─" * 59)
    def bucket(m):
        if m is None:
            return "n/a"
        return ("<0.15R" if m < 0.15 else "0.15-0.30R" if m < 0.30
                else "0.30-0.60R" if m < 0.60 else ">0.60R")
    g = _group([{**r, "_b": bucket(r["mfe_r"])} for r in rows], "_b")
    for k in ["<0.15R", "0.15-0.30R", "0.30-0.60R", ">0.60R", "n/a"]:
        if k in g:
            print(f"  {k:<16} {_agg(g[k])}")
    print()

    # NOTE: mfe_r/mae_r use the DISASTER-FLOOR denominator (a stable cross-trade
    # frame) while realized_r uses the tighter placed-SL frame — different R units,
    # so realized_r / mfe_r is NOT a meaningful capture ratio. MFE/MAE are for
    # RANKING entry quality (the bucket table above), not for capture math.
    noise = [r for r in rows if r["mfe_r"] is not None and r["mfe_r"] < 0.15]
    if rows:
        print(f"Entry-quality flag: {len(noise)}/{len(rows)} "
              f"({100*len(noise)/len(rows):.0f}%) of entries peak < 0.15R in favor "
              f"→ {'ENTRY FILTER is the top lever' if len(noise) > len(rows)*0.4 else 'acceptable'}")


def main() -> None:
    rows = build()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        cols = list(rows[0].keys())
        with OUT_CSV.open("w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    report(rows)
    print(f"\n[written] {OUT_CSV}  ({len(rows)} rows × {len(rows[0]) if rows else 0} cols)")


if __name__ == "__main__":
    main()
