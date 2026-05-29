"""GET /dashboard/state — combined snapshot for the React dashboard.

Returns bars (OHLC + delta), VP levels, M2 votes, latest M1 decision,
open positions, and A/B comparison stats in one shot.

GET /dashboard/state?symbol=BTCUSDT&tf=1m&minutes=120
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib import request as _urlreq

from flask import Blueprint, current_app, jsonify, request

from pipeline.state_store import store as _state_store
import pipeline.features.vp_cache as vp_cache
from execution.direction_engine import decide_direction
from execution.position_store import position_store, position_store_m2

bp = Blueprint("dashboard", __name__)
LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DECISIONS_LOG = ROOT / "data" / "decisions.jsonl"
POSITIONS_LOG = ROOT / "data" / "positions.jsonl"
POSITIONS_LOG_M2 = ROOT / "data" / "positions_m2.jsonl"

# ── helpers ──────────────────────────────────────────────────────────────────

def _latest_m1_decision(symbol: str) -> dict | None:
    """Read last matching decision from decisions.jsonl."""
    if not DECISIONS_LOG.exists():
        return None
    last = None
    try:
        with DECISIONS_LOG.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("symbol") == symbol:
                    last = row
    except Exception as e:
        LOG.warning(f"[dashboard] decisions.jsonl read failed: {e}")
    if not last:
        return None
    dec = last.get("decision") or {}
    return {
        "ts": last.get("ts", 0),
        "side": dec.get("side", "flat"),
        "entry": dec.get("entry"),
        "stop_loss": dec.get("stop_loss"),
        "take_profit": dec.get("take_profit"),
        "confidence": dec.get("confidence", 0),
        "bias_strength": dec.get("bias_strength", 1),
        "rationale": dec.get("rationale", ""),
        "validator_reason": last.get("validator_reason"),
    }


def _ab_stats(symbols: list[str]) -> dict:
    """Compute WR + total_R from positions.jsonl (M1) and positions_m2.jsonl (M2)."""
    def _scan(path: Path) -> dict[str, list[float]]:
        results: dict[str, list[float]] = {}
        if not path.exists():
            return results
        try:
            with path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") != "close":
                        continue
                    # Reconstruct symbol from position — need to scan open events
                    pass
        except Exception:
            pass
        return results

    # Simpler: query position_store directly (already in memory)
    def _stats_from_store(pstore) -> dict[str, dict]:
        out: dict[str, dict] = {}
        all_pos = list(pstore._positions.values())
        for sym in symbols:
            sym_pos = [p for p in all_pos if p.symbol == sym and p.status in ("closed", "invalidated")]
            n = len(sym_pos)
            wins = sum(1 for p in sym_pos if p.realized_r > 0)
            total_r = sum(p.realized_r for p in sym_pos)
            out[sym] = {
                "n": n,
                "wins": wins,
                "wr": round(wins / n, 2) if n else 0.0,
                "total_r": round(total_r, 2),
            }
        return out

    result: dict = {}
    try:
        m1 = _stats_from_store(position_store())
        m2 = _stats_from_store(position_store_m2())
        for sym in symbols:
            result[sym] = {"m1": m1.get(sym, {"n": 0, "wins": 0, "wr": 0.0, "total_r": 0.0}),
                           "m2": m2.get(sym, {"n": 0, "wins": 0, "wr": 0.0, "total_r": 0.0})}
    except Exception as e:
        LOG.warning(f"[dashboard] ab_stats failed: {e}")

    # 24h outcomes from M1 store
    outcomes_24h = {"tp_hit": 0, "sl_hit": 0, "invalidated": 0, "sum_r": 0.0}
    try:
        cutoff = int(time.time()) - 86400
        for p in position_store()._positions.values():
            if p.closed_ts and p.closed_ts >= cutoff:
                reason = p.close_reason or ""
                if "tp" in reason:
                    outcomes_24h["tp_hit"] += 1
                elif "sl" in reason or "escape" in reason:
                    outcomes_24h["sl_hit"] += 1
                elif "invalid" in reason or "choch" in reason or "va_break" in reason:
                    outcomes_24h["invalidated"] += 1
                outcomes_24h["sum_r"] = round(outcomes_24h["sum_r"] + p.realized_r, 2)
    except Exception:
        pass

    result["outcomes_24h"] = outcomes_24h
    return result


def _build_detections(symbol: str, tf: str) -> dict:
    """Run lightweight detectors on the latest bar — no Claude call."""
    detections: dict = {
        "fvgs": [],
        "sweep": None,
        "active_sweeps": [],
        "absorptions": [],
        "wave": None,
        "day_type": None,
        "big_trades": [],
    }
    try:
        from pipeline.state_store import store as _st
        bars = _st().recent(symbol, tf, 100)
        if not bars:
            return detections
        latest = bars[-1]

        # FVGs
        try:
            from pipeline.features.fvg import detect_fvgs
            fvgs = detect_fvgs(bars[-20:])
            detections["fvgs"] = [
                {"side": f.side, "low": f.low, "high": f.high, "formed_at_ts": f.formed_at_ts}
                for f in (fvgs or [])
            ]
        except Exception:
            pass

        # Sweep
        try:
            from pipeline.features.sweep import detect
            sw = detect(bars, symbol)
            if sw and sw.type != "none":
                detections["sweep"] = {
                    "type": sw.type,
                    "wick_extreme": sw.wick_extreme,
                    "level_label": sw.level_label,
                    "confidence": round(sw.confidence, 2),
                    "pattern": sw.pattern,
                    "age_bars": sw.age_bars,
                    "granularity": getattr(sw, "granularity", ""),
                }
        except Exception:
            pass

        # Absorptions — new close-vs-heavy-node detector + wick-trap flag
        try:
            from pipeline.features.absorption import detect_close_failure_absorption
            from pipeline.footprint import build as _fp_build
            abs_list = detect_close_failure_absorption(latest, _fp_build(latest))
            if abs_list:
                detections["absorptions"] = [
                    {"price": a.price, "side": a.side, "volume": round(a.volume, 2),
                     "bar_pct": round(a.bar_pct, 2), "is_wick_trap": a.is_wick_trap}
                    for a in abs_list
                ]
        except Exception:
            pass

        # Wave
        try:
            from pipeline.features.wave import classify as wave_classify
            w = wave_classify(bars)
            if w:
                detections["wave"] = {
                    "phase": w.phase,
                    "direction": w.direction,
                    "confidence": round(w.confidence, 2),
                    "fib_retrace": {str(k): round(v, 2) for k, v in (w.fib_retrace or {}).items()},
                    "wave_label": w.wave_label,
                }
        except Exception:
            pass

        # Day type
        try:
            from pipeline.features.day_type import classify as dt_classify
            dt = dt_classify(bars, symbol)
            if dt:
                detections["day_type"] = {
                    "type": dt.type,
                    "confidence": round(dt.confidence, 2),
                    "grid_mode": dt.grid_mode,
                }
        except Exception:
            pass

        # Active sweep registry (all live sweeps, not just the latest detect())
        try:
            from pipeline.features.sweep import active_sweeps
            detections["active_sweeps"] = [
                {
                    "sweep_type":    ev.sweep_type,
                    "swept_level":   round(ev.swept_level, 4),
                    "level_label":   ev.level_label,
                    "wick_extreme":  round(ev.wick_extreme, 4),
                    "initial_close": round(ev.initial_close, 4),
                    "confidence":    round(ev.confidence, 2),
                    "delta_confirms": ev.delta_confirms,
                    "granularity":   ev.granularity,
                    "age_bars":      ev.age_bars,
                    "pattern":       ev.pattern,
                    "stale":         ev.stale,
                }
                for ev in (active_sweeps(symbol) or [])
            ]
        except Exception:
            pass

        # Big-trade events with resolved outcomes (last N)
        try:
            from pipeline.features.big_trade import get_recent_events
            detections["big_trades"] = [
                {
                    "ts":              ev.ts,
                    "price":           round(ev.price, 4),
                    "volume":          round(ev.volume, 4),
                    "aggressor":       ev.aggressor,
                    "vp_context":      ev.vp_context,
                    "outcome":         ev.outcome,
                    "price_moved_pct": round(ev.price_moved_pct, 4),
                    "sweep_associated": ev.sweep_associated,
                }
                for ev in (get_recent_events(symbol, tf, n=20) or [])
            ]
        except Exception:
            pass

    except Exception as e:
        LOG.warning(f"[dashboard] detections failed: {e}")

    return detections


# ── live bar ─────────────────────────────────────────────────────────────────

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}
_BINANCE_SYM_MAP = {"XAUTUSDT": "XAUUSDT"}


def _infer_tick(symbol: str, price: float) -> float:
    """Ladder bucket size — MUST match the closed-bar feeder's --price-step
    in scripts/start.sh, otherwise the live bar's ladder won't line up with
    the closed bars that replace it.
    """
    if symbol.startswith("BTC"):
        return 10.0   # matches binance.main --price-step 10.0
    if symbol.startswith("ETH"):
        return 1.0
    if symbol.startswith("XAU"):
        return 0.1    # matches binance.main --price-step 0.1
    return max(round(price * 0.00002, 6), 0.001)


def _live_ladders(binance_sym: str, open_ms: int, symbol: str, ref_price: float,
                  include_ladder: bool) -> tuple[list, list]:
    """Aggregate Binance Futures aggTrades since bar open into bid/ask ladders.

    Returns (bid_ladder, ask_ladder) where each is a list of {"p": price, "v": vol}.
    Buyer-maker (m=true)  -> seller hit the bid -> bid_ladder.
    Buyer-taker (m=false) -> buyer lifted ask  -> ask_ladder.
    """
    if not include_ladder:
        return [], []
    tick = _infer_tick(symbol, ref_price)
    bid: dict[int, float] = {}
    ask: dict[int, float] = {}
    cursor = open_ms
    deadline = time.time() + 2.0  # cap total time spent
    try:
        for _ in range(20):  # safety: max 20 pages
            url = (
                f"https://fapi.binance.com/fapi/v1/aggTrades?"
                f"symbol={binance_sym}&startTime={cursor}&limit=1000"
            )
            with _urlreq.urlopen(url, timeout=3) as resp:
                trades = json.loads(resp.read().decode())
            if not trades:
                break
            for t in trades:
                price = float(t["p"])
                qty = float(t["q"])
                key = round(price / tick)
                if t.get("m"):
                    bid[key] = bid.get(key, 0.0) + qty
                else:
                    ask[key] = ask.get(key, 0.0) + qty
            if len(trades) < 1000:
                break
            cursor = int(trades[-1]["T"]) + 1
            if time.time() > deadline:
                break
    except Exception as e:
        LOG.debug(f"[dashboard] live_ladders failed: {e}")
        return [], []

    def _serialize(d: dict[int, float]) -> list:
        return [{"p": round(k * tick, 6), "v": round(v, 4)} for k, v in sorted(d.items())]

    return _serialize(bid), _serialize(ask)


def _live_bar(symbol: str, tf: str, include_ladder: bool = False) -> dict | None:
    """Fetch the current open (incomplete) kline from Binance Futures REST."""
    binance_sym = _BINANCE_SYM_MAP.get(symbol, symbol)
    interval = tf  # 1m / 5m / 15m map 1:1
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_sym}&interval={interval}&limit=2"
    try:
        with _urlreq.urlopen(url, timeout=4) as resp:
            klines = json.loads(resp.read().decode())
        if not klines:
            return None
        k = klines[-1]
        tf_sec = _TF_SECONDS.get(tf, 60)
        open_ms = k[0]
        open_ts_s = open_ms // 1000
        close_ts = open_ts_s + tf_sec
        taker_buy = float(k[9])
        total_vol = float(k[5])
        bid_vol = round(total_vol - taker_buy, 4)
        ask_vol = round(taker_buy, 4)
        out = {
            "ts": close_ts,
            "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]),
            "delta": round(ask_vol - bid_vol, 4),
            "poc": None,
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "live": True,
        }
        if include_ladder:
            bl, al = _live_ladders(binance_sym, open_ms, symbol, float(k[4]), True)
            out["bid_ladder"] = bl
            out["ask_ladder"] = al
            # Approximate POC from live ladder (max total per price)
            totals: dict[float, float] = {}
            for lvl in bl:
                totals[lvl["p"]] = totals.get(lvl["p"], 0.0) + lvl["v"]
            for lvl in al:
                totals[lvl["p"]] = totals.get(lvl["p"], 0.0) + lvl["v"]
            if totals:
                out["poc"] = max(totals.items(), key=lambda kv: kv[1])[0]
        return out
    except Exception as e:
        LOG.debug(f"[dashboard] live_bar failed: {e}")
        return None


# ── routes ───────────────────────────────────────────────────────────────────

@bp.get("/dashboard/state")
def dashboard_state():
    settings = current_app.config["FB_SETTINGS"]
    symbol = request.args.get("symbol") or settings["instrument"]["symbol"]
    tf = request.args.get("tf") or "1m"
    minutes = int(request.args.get("minutes", 120))
    minutes = max(5, min(minutes, 1440))
    include_fp = request.args.get("footprint") == "true"
    session_align = request.args.get("session") == "today"

    # Bars — fetch enough closed bars to cover the window
    s = _state_store()
    tf_sec = _TF_SECONDS.get(tf, 60)
    bar_count = (minutes * 60 // tf_sec) + 10
    if session_align:
        # Pull a wide net then cut to session boundary (XAU 03:30 IST / BTC 05:30 IST)
        bar_count = max(bar_count, 24 * 60 // tf_sec + 10)
    raw_bars = s.recent(symbol, tf, bar_count)
    if session_align:
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _IST2 = _tz2(_td2(hours=5, minutes=30))
        _now = _dt2.now(_IST2)
        _hr = 3 if symbol.startswith("XAU") else 5
        _cand = _now.replace(hour=_hr, minute=30, second=0, microsecond=0)
        if _now < _cand:
            _cand -= _td2(days=1)
        cutoff_ts = int(_cand.timestamp())
    else:
        cutoff_ts = (raw_bars[-1].close_ts - minutes * 60) if raw_bars else 0
    raw_bars = [b for b in raw_bars if b.close_ts >= cutoff_ts]

    bars = []
    for b in raw_bars:
        bid_vol = sum(lvl.vol for lvl in b.bid_ladder)
        ask_vol = sum(lvl.vol for lvl in b.ask_ladder)
        bar_data = {
            "ts": b.close_ts,
            "o": b.ohlc.o,
            "h": b.ohlc.h,
            "l": b.ohlc.l,
            "c": b.ohlc.c,
            "delta": round(b.delta or 0, 4),
            "poc": b.poc,
            "bid_vol": round(bid_vol, 2),
            "ask_vol": round(ask_vol, 2),
        }
        if include_fp:
            bar_data["bid_ladder"] = [{"p": round(lvl.price, 4), "v": round(lvl.vol, 4)} for lvl in b.bid_ladder]
            bar_data["ask_ladder"] = [{"p": round(lvl.price, 4), "v": round(lvl.vol, 4)} for lvl in b.ask_ladder]
        bars.append(bar_data)

    # Append live (in-progress) bar
    live = _live_bar(symbol, tf, include_ladder=include_fp)
    if live and (not bars or live["ts"] > bars[-1]["ts"]):
        bars.append(live)

    # VP
    daily_vp = vp_cache.get(symbol, "daily") or {}

    # Daily VP histogram: session-aligned per symbol.
    #   XAUTUSDT: 03:30 IST → 02:29 IST next day (CME EDT session)
    #   BTCUSDT:  05:30 IST → 05:29 IST (24h, UTC midnight aligned)
    # Bars before session_start belong to yesterday's session.
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _IST = _tz(_td(hours=5, minutes=30))
        now_ist = _dt.now(_IST)
        session_hr_ist = 3 if symbol.startswith("XAU") else 5
        session_min_ist = 30
        candidate = now_ist.replace(hour=session_hr_ist, minute=session_min_ist,
                                    second=0, microsecond=0)
        if now_ist < candidate:
            candidate = candidate - _td(days=1)
        session_start_ts = int(candidate.timestamp())
        day_bars = s.recent(symbol, tf, 2000)
        day_bars = [b for b in day_bars if b.close_ts >= session_start_ts]
        if day_bars:
            day_low  = min(b.ohlc.l for b in day_bars)
            day_high = max(b.ohlc.h for b in day_bars)
            day_range = max(day_high - day_low, 0.0001)
            VP_BINS = 40
            bin_size = day_range / VP_BINS
            buckets: dict[int, float] = {}
            for b in day_bars:
                for lvl in b.bid_ladder:
                    if lvl.vol > 0:
                        k = int((lvl.price - day_low) / bin_size)
                        buckets[k] = buckets.get(k, 0.0) + lvl.vol
                for lvl in b.ask_ladder:
                    if lvl.vol > 0:
                        k = int((lvl.price - day_low) / bin_size)
                        buckets[k] = buckets.get(k, 0.0) + lvl.vol
            histogram = [
                {"p": round(day_low + (k + 0.5) * bin_size, 4),
                 "v": round(v, 4)}
                for k, v in sorted(buckets.items())
            ]
            daily_vp["histogram"] = histogram
            daily_vp["bin_size"]  = round(bin_size, 4)
            daily_vp["session_start_ts"] = session_start_ts
    except Exception as e:
        LOG.debug(f"[dashboard] vp histogram failed: {e}")
    prior_vp_list = vp_cache.get_history(symbol, "daily", n=2)
    prior_vp = prior_vp_list[-2] if len(prior_vp_list) >= 2 else {}

    # M2 direction (calls all module voters — cheap, no Claude)
    primary_tf = settings["instrument"].get("primary_tf", "15m")
    m2_decision = decide_direction(symbol, primary_tf)
    latest_m2 = {
        "ts": int(time.time()),
        "side": m2_decision.side,
        "score": round(m2_decision.score, 3),
        "bias_strength": m2_decision.bias_strength,
        "votes": [
            {"module": v.module, "direction": round(v.direction, 2),
             "strength": round(v.strength, 2), "reason": v.reason}
            for v in m2_decision.votes
        ],
        "note": m2_decision.note,
    }

    # M1 latest decision
    latest_m1 = _latest_m1_decision(symbol)

    # Detections
    detections = _build_detections(symbol, tf)

    # Open positions
    open_pos = position_store().open_positions()
    positions_out = []
    # Mechanical lots ladder used by grid_placer (FIB scaled by base_lot + bias)
    _FIB = (1, 1, 2, 3, 5)
    _base_lots_cfg = (settings.get("execution", {}).get("default_lots") or {})
    base_lot = float(_base_lots_cfg.get(symbol, 0.01))
    for p in open_pos:
        if p.symbol != symbol:
            continue
        leg_prices = [leg.entry for leg in p.legs]
        legs_detail = []
        for leg in p.legs:
            mult = _FIB[leg.leg - 1] if leg.leg - 1 < len(_FIB) else 1
            est_lots = round(base_lot * mult, 4)
            actual_lots = float(getattr(leg, "lots", 0.0) or 0.0)
            legs_detail.append({
                "leg":         leg.leg,
                "entry":       round(leg.entry, 4),
                "lots":        round(actual_lots, 4) if actual_lots > 0 else est_lots,
                "lots_source": "actual" if actual_lots > 0 else "estimated",
                "stop_loss":   round(leg.stop_loss, 4),
                "take_profit": round(leg.take_profit, 4),
                "opened_ts":   leg.opened_ts,
                "confidence":  round(leg.confidence, 2),
                "rationale":   leg.rationale,
            })
        total_lots = round(sum(l["lots"] for l in legs_detail), 4)
        positions_out.append({
            "position_id": p.position_id,
            "symbol": p.symbol,
            "side": p.side,
            "avg_entry": round(p.avg_entry, 4),
            "take_profit": round(p.take_profit, 4),
            "stop_loss": round(p.stop_loss, 4),
            "legs_filled": p.leg_count,
            "leg_prices": leg_prices,
            "legs": legs_detail,
            "total_lots": total_lots,
            "opened_ts": p.opened_ts,
        })

    # Closed positions for this symbol (last 50). Used for chart trade-history.
    closed_out: list[dict] = []
    try:
        for p in position_store().closed_positions(symbol, n=50):
            leg_prices = [leg.entry for leg in p.legs]
            last_leg = p.legs[-1] if p.legs else None
            # Exit price = last-leg TP or SL depending on close_reason
            reason = (p.close_reason or "").lower()
            if "tp" in reason and last_leg:
                exit_price = last_leg.take_profit
            elif ("sl" in reason or "stop" in reason) and last_leg:
                exit_price = last_leg.stop_loss
            else:
                exit_price = last_leg.entry if last_leg else p.avg_entry
            closed_out.append({
                "position_id": p.position_id,
                "side":         p.side,
                "avg_entry":    round(p.avg_entry, 4),
                "exit_price":   round(exit_price, 4),
                "stop_loss":    round(p.stop_loss, 4),
                "take_profit":  round(p.take_profit, 4),
                "leg_prices":   leg_prices,
                "opened_ts":    p.opened_ts,
                "closed_ts":    p.closed_ts,
                "close_reason": p.close_reason,
                "realized_r":   round(p.realized_r, 4),
                "status":       p.status,
            })
    except Exception as e:
        LOG.debug(f"[dashboard] closed_positions failed: {e}")

    # Pending orders for this symbol
    pending_out: list[dict] = []
    try:
        from execution.pending_orders import pending_store
        for po in pending_store().open_for(symbol):
            pending_out.append({
                "pending_id": po.pending_id,
                "position_id": po.position_id,
                "side":        po.side,
                "limit_price": round(po.limit_price, 4),
                "lots":        po.lots,
                "leg_idx":     po.leg_idx,
                "tp":          round(po.tp, 4),
                "safety_sl":   round(po.safety_sl, 4) if po.safety_sl is not None else None,
                "state":       po.state,
            })
    except Exception as e:
        LOG.debug(f"[dashboard] pending_orders failed: {e}")

    # CVD candle series + dominant CVD signal
    cvd_candles: list[dict] = []
    cvd_signal: dict | None = None
    try:
        from pipeline.features.cvd_candlestick import build_cvd_candles, detect as detect_cvd
        cvd_objs = build_cvd_candles(raw_bars)
        cvd_candles = [
            {"ts": c.ts, "o": round(c.cvd_open, 4), "h": round(c.cvd_high, 4),
             "l": round(c.cvd_low, 4), "c": round(c.cvd_close, 4),
             "delta": round(c.delta, 4)}
            for c in cvd_objs
        ]
        sig = detect_cvd(raw_bars)
        if sig and sig.pattern != "none":
            cvd_signal = {
                "pattern":   sig.pattern,
                "side":      sig.side,
                "confidence": round(sig.confidence, 2),
                "reference_level": round(sig.reference_level, 4),
                "reason":    sig.reason,
            }
    except Exception as e:
        LOG.debug(f"[dashboard] cvd_candles failed: {e}")

    # Strong zones (TP/SL targets) — long + short ladders around current price
    zones_out: dict = {"long": [], "short": []}
    try:
        from execution.zone_collector import collect as collect_zones
        if raw_bars:
            anchor = float(raw_bars[-1].ohlc.c)
            for direction in ("long", "short"):
                zs = collect_zones(symbol, direction, anchor, htf_bars=raw_bars, n=5)
                zones_out[direction] = [
                    {"price": round(z.price, 4), "source": z.source,
                     "strength": round(z.strength, 2)}
                    for z in (zs or [])
                ]
    except Exception as e:
        LOG.debug(f"[dashboard] zones failed: {e}")

    # A/B stats
    all_symbols = list({p.symbol for p in position_store()._positions.values()} | {"BTCUSDT", "XAUTUSDT"})
    ab_stats = _ab_stats(all_symbols)

    # CORS for Vite dev server
    resp = jsonify({
        "symbol": symbol,
        "tf": tf,
        "bars": bars,
        "daily_vp": daily_vp,
        "prior_vp": prior_vp,
        "detections": detections,
        "latest_m1": latest_m1,
        "latest_m2": latest_m2,
        "positions": {"open": positions_out, "pending": pending_out, "closed": closed_out},
        "cvd": cvd_candles,
        "cvd_signal": cvd_signal,
        "zones": zones_out,
        "ab_stats": ab_stats,
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp
