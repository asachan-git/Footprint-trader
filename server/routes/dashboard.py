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

# Big-trade JSONL cache — keyed by file path. Stores parsed events per symbol
# + the last byte offset, so subsequent calls only read appended bytes.
_BT_CACHE: dict = {"path": None, "size": 0, "by_symbol": {}, "mtime": 0.0}

def _big_trade_raw_cached(path, symbol: str) -> list[dict]:
    """Return cached raw events for `symbol`. Re-parses only newly-appended bytes."""
    import json as _json
    if not path.exists():
        return []
    st = path.stat()
    cur_size, cur_mtime = st.st_size, st.st_mtime
    # Invalidate cache if file shrank / rotated / different path
    if (_BT_CACHE["path"] != str(path) or cur_size < _BT_CACHE["size"]
            or cur_mtime < _BT_CACHE["mtime"]):
        _BT_CACHE["path"] = str(path)
        _BT_CACHE["size"] = 0
        _BT_CACHE["by_symbol"] = {}
    start = _BT_CACHE["size"]
    if cur_size > start:
        with path.open("rb") as fh:
            fh.seek(start)
            tail = fh.read().decode(errors="ignore")
        # If we resumed mid-line (unlikely with line-buffered appends) drop it
        if start > 0 and not tail.startswith("\n") and "\n" in tail:
            tail = tail[tail.index("\n") + 1:]
        for line in tail.splitlines():
            if not line.strip():
                continue
            try:
                e = _json.loads(line)
            except Exception:
                continue
            sym = e.get("symbol") or ""
            _BT_CACHE["by_symbol"].setdefault(sym, []).append(e)
        _BT_CACHE["size"] = cur_size
        _BT_CACHE["mtime"] = cur_mtime
    return _BT_CACHE["by_symbol"].get(symbol, [])


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

        # Sweep — detect on latest bar using cached swing points (rebuild if cache cold)
        try:
            from pipeline.features.sweep import detect as _sw_detect
            from pipeline.features.swing import get as _swing_get, build as _swing_build
            _sp = _swing_get(symbol)
            if _sp is None:
                _sp = _swing_build(symbol, tf, bars[-100:])
            if _sp is not None:
                sw = _sw_detect(latest, _sp, bars[-100:])
                if sw and sw.type != "none":
                    detections["sweep"] = {
                        "type": sw.type,
                        "wick_extreme": sw.wick_extreme,
                        "level_label": sw.level_label,
                        "confidence": round(sw.confidence, 2),
                        "pattern": getattr(sw, "pattern", ""),
                        "age_bars": getattr(sw, "age_bars", 0),
                        "granularity": getattr(sw, "granularity", "candle"),
                        "classification": getattr(sw, "classification", ""),
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

        # Coup / coup_reversal absorption candles — scan the whole window with the
        # strategies' own trigger detector (detect_canonical_absorption) so the chart
        # marks every candle coup/coup_reversal would treat as an absorption trigger,
        # not just the latest bar. Mirrors coup._find_trigger gating: vol ≥ vol_mult×
        # rolling-median total, momentum also gated on |delta|/vol; reversal isn't.
        try:
            from pipeline.features.absorption import detect_canonical_absorption
            from pipeline.footprint import build as _fp_build
            from pipeline.features.atr import atr as _atr
            from statistics import median as _median
            _CONT_N, _CONT_ATR = 6, 1.0   # winner-dir move >= 1xATR within 6 bars = continuation

            def _extreme_split(b, winner):
                """Aggressive buy(ask)/sell(bid) vol in the absorbed extreme 10% zone."""
                h, l = b.ohlc.h, b.ohlc.l
                rng = max(h - l, 1e-9)
                zone = rng * 0.10
                if winner == "short":
                    thr = h - zone
                    ask = sum(v.vol for v in b.ask_ladder if v.price >= thr)
                    bid = sum(v.vol for v in b.bid_ladder if v.price >= thr)
                else:
                    thr = l + zone
                    ask = sum(v.vol for v in b.ask_ladder if v.price <= thr)
                    bid = sum(v.vol for v in b.bid_ladder if v.price <= thr)
                tot = ask + bid
                return (round(ask, 2), round(bid, 2),
                        ("buy" if ask >= bid else "sell"),
                        (round(ask / tot, 3) if tot > 0 else None))

            # Wider, deduped window so markers span the full loaded chart history
            # (the shared 100-bar `bars` only covers ~25h of 15m). store() globs
            # main + *_agg files (dup close_ts) → dedupe keeping the richer ladder.
            _raw = _st().recent(symbol, tf, 3000)
            _byts: dict[int, object] = {}
            for _b in _raw:
                ex = _byts.get(_b.close_ts)
                if ex is None or (len(_b.bid_ladder) + len(_b.ask_ladder)) > (len(ex.bid_ladder) + len(ex.ask_ladder)):
                    _byts[_b.close_ts] = _b
            cbars = [_byts[k] for k in sorted(_byts)]

            def _scan(mode: str, vol_mult: float, delta_ratio: float, vol_lb: int):
                fps = [_fp_build(b) for b in cbars]
                tots = [(fp.total_bid + fp.total_ask) for fp in fps]
                gate_delta = mode != "reversal"
                out = []
                for i, b in enumerate(cbars):
                    prior = [t for t in tots[max(0, i - vol_lb):i] if t > 0]
                    if not prior:
                        continue
                    med = _median(prior)
                    if med <= 0 or tots[i] < vol_mult * med:
                        continue
                    if gate_delta and (b.delta is None
                                       or abs(b.delta) / max(tots[i], 1e-9) < delta_ratio):
                        continue
                    for a in detect_canonical_absorption(b, fps[i], mode=mode):
                        winner = "long" if a.side == "buy" else "short"
                        zask, zbid, zagg, zbf = _extreme_split(b, winner)
                        # next-candle reversal confirmation (if a next bar exists)
                        nxt = cbars[i + 1] if i + 1 < len(cbars) else None
                        nconf = None
                        ndelta = nxt.delta if nxt else None
                        if nxt is not None:
                            ntot = tots[i + 1]
                            nd = nxt.delta or 0.0
                            ndr = abs(nd) / max(ntot, 1e-9)
                            dwith = (nd > 0) if winner == "long" else (nd < 0)
                            prog = (nxt.ohlc.c > b.ohlc.c) if winner == "long" else (nxt.ohlc.c < b.ohlc.c)
                            nconf = bool(dwith and ndr >= 0.25 and prog)
                        # forward outcome: MFE/MAE in ATR over next _CONT_N bars
                        atr_v = _atr(cbars[:i + 1]) or 0.0
                        fwd = cbars[i + 1:i + 1 + _CONT_N]
                        c0 = b.ohlc.c
                        mfe_atr = mae_atr = None
                        cont = None
                        if fwd and atr_v > 0:
                            if winner == "long":
                                mfe = max(x.ohlc.h for x in fwd) - c0
                                mae = c0 - min(x.ohlc.l for x in fwd)
                            else:
                                mfe = c0 - min(x.ohlc.l for x in fwd)
                                mae = max(x.ohlc.h for x in fwd) - c0
                            mfe_atr = round(mfe / atr_v, 2)
                            mae_atr = round(mae / atr_v, 2)
                            cont = bool(mfe >= _CONT_ATR * atr_v)
                        out.append({
                            "ts": b.close_ts,
                            "winner": winner,
                            "trapped": "buyers@top" if winner == "short" else "sellers@low",
                            "price": a.price,
                            "bar_dir": "bull" if b.ohlc.c > b.ohlc.o else ("bear" if b.ohlc.c < b.ohlc.o else "doji"),
                            "vol_ratio": round(tots[i] / med, 2),
                            "delta": round(b.delta or 0, 2),
                            "bar_pct": round(a.bar_pct, 2),
                            "is_wick_trap": a.is_wick_trap,
                            "zone_ask": zask, "zone_bid": zbid,
                            "zone_aggressor": zagg, "zone_buy_frac": zbf,
                            "next_confirms": nconf,
                            "next_delta": round(ndelta, 2) if ndelta is not None else None,
                            "fwd_mfe_atr": mfe_atr, "fwd_mae_atr": mae_atr,
                            "continuation": cont,
                        })
                return out

            detections["coup_absorptions"] = _scan("momentum", 1.8, 0.35, 20)
            detections["coup_reversal_absorptions"] = _scan("reversal", 1.8, 0.35, 10)
        except Exception as e:
            LOG.debug(f"[dashboard] coup absorptions failed: {e}")

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

        # VA width regime — expanding/contracting across last 3 sessions
        try:
            from pipeline.features.vp_cache import get_va_regime as _va_regime
            detections["va_regime"] = _va_regime(symbol, "daily", n=3)
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
                    "classification": ev.classification,
                    "stale":         ev.stale,
                }
                for ev in (active_sweeps(symbol) or [])
            ]
        except Exception:
            pass

        # Big-trade events — cached merged dict per (symbol). File is append-only,
        # so we incrementally parse new bytes since last read instead of re-reading
        # the full JSONL on every poll (~23k lines → 2s+ scan).
        try:
            from pipeline.features.big_trade import BIG_TRADES_LOG
            import json as _json
            raw = _big_trade_raw_cached(BIG_TRADES_LOG, symbol)
            # Server-side merge is TIGHT (1-minute × 1-tick) so per-minute
            # granularity preserved. Client re-merges visually by zoom level.
            tbin = 60
            _ref_px = float(raw[-1].get("price") or 0) if raw else 0.0
            tick = _infer_tick(symbol, _ref_px) if _ref_px else (10.0 if symbol.startswith("BTC") else 0.1)
            pbin = max(tick, 1e-9)
            buckets: dict[tuple, dict] = {}
            for e in raw:
                ts = int(e.get("ts") or 0)
                px = float(e.get("price") or 0)
                key = (ts // tbin, round(px / pbin), e.get("aggressor"))
                cur = buckets.get(key)
                if cur is None:
                    buckets[key] = {
                        "ts":              ts,
                        "price":           round(px, 4),
                        "volume":          float(e.get("volume") or 0),
                        "aggressor":       e.get("aggressor"),
                        "vp_context":      e.get("vp_context"),
                        "outcome":         e.get("outcome") or "pending",
                        "price_moved_pct": float(e.get("price_moved_pct") or 0),
                        "sweep_associated": bool(e.get("sweep_associated")),
                        "merged_count":    1,
                    }
                else:
                    cur["volume"] += float(e.get("volume") or 0)
                    cur["merged_count"] += 1
                    # Keep most-progressed outcome
                    rank = {"pending": 0, "pushed": 2, "absorbed": 1, "exhausted": 1}
                    if rank.get(e.get("outcome") or "pending", 0) > rank.get(cur["outcome"], 0):
                        cur["outcome"] = e.get("outcome")
                    if abs(float(e.get("price_moved_pct") or 0)) > abs(cur["price_moved_pct"]):
                        cur["price_moved_pct"] = float(e.get("price_moved_pct") or 0)
                    if e.get("sweep_associated"):
                        cur["sweep_associated"] = True
            merged = sorted(buckets.values(), key=lambda x: x["ts"])
            for m in merged:
                m["volume"] = round(m["volume"], 4)
                m["price_moved_pct"] = round(m["price_moved_pct"], 4)
            detections["big_trades"] = merged[-3000:]
        except Exception as e:
            LOG.debug(f"[dashboard] big_trades read failed: {e}")

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


# Short-TTL cache for live REST calls — many concurrent polls hit cache
_LIVE_CACHE: dict = {}   # key=(symbol,tf,include_ladder) -> (ts, payload)
_LIVE_TTL = 3.5          # seconds — > poll cadence so warm cache served
_REQUEST_LOG: dict = {}  # key -> last_request_ts (lazy prefetch tracking)
_PREFETCH_STARTED = False

def _start_lazy_prefetch():
    """Refresh ONLY the live keys requested in the last 30s. Avoids speculative
    prefetching of stale combos. Route NEVER blocks on REST when prefetcher
    is running — always served from cache."""
    global _PREFETCH_STARTED
    if _PREFETCH_STARTED:
        return
    _PREFETCH_STARTED = True
    import threading, time as _time
    def _loop():
        while True:
            now = _time.time()
            keys = [k for k, ts in list(_REQUEST_LOG.items()) if now - ts < 30]
            for sym, tf, lad in keys:
                try:
                    _live_bar_fetch(sym, tf, lad)
                except Exception:
                    pass
            _time.sleep(1.5)
    t = threading.Thread(target=_loop, name="live-prefetch", daemon=True)
    t.start()

def _live_bar(symbol: str, tf: str, include_ladder: bool = False) -> dict | None:
    """Return cached live bar. On cache miss returns stale data if available,
    else does a one-shot fetch. Prefetcher keeps cache warm."""
    import time as _time
    cache_key = (symbol, tf, include_ladder)
    now = _time.time()
    cached = _LIVE_CACHE.get(cache_key)
    if cached and (now - cached[0] < _LIVE_TTL):
        return cached[1]
    if cached:
        # Return stale rather than block route on REST. Prefetcher refreshes async.
        return cached[1]
    return _live_bar_fetch(symbol, tf, include_ladder)


def _live_bar_fetch(symbol: str, tf: str, include_ladder: bool = False) -> dict | None:
    """Actual REST fetch — used by prefetcher and one-shot cold-cache fallback."""
    import time as _time
    cache_key = (symbol, tf, include_ladder)
    now = _time.time()
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
        _LIVE_CACHE[cache_key] = (now, out)
        return out
    except Exception as e:
        LOG.debug(f"[dashboard] live_bar failed: {e}")
        return None


# ── routes ───────────────────────────────────────────────────────────────────

def _build_snapshot(settings: dict, symbol: str, tf: str, minutes: int,
                    include_fp: bool, session_align: bool) -> dict:
    """Build the full dashboard snapshot dict. Shared by /dashboard/state
    (JSON poll) and /dashboard/stream (SSE)."""
    _start_lazy_prefetch()

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

    # Append live (in-progress) bar — also tag this key for lazy prefetcher
    import time as _t_now
    _REQUEST_LOG[(symbol, tf, include_fp)] = _t_now.time()
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

    # Multi-session naked POCs — untested prior-session POCs as TP magnets
    naked_pocs_out: list[dict] = []
    try:
        from pipeline.features.naked_poc import get_naked_pocs as _get_npoc
        _ref_price = float(daily_vp.get("poc") or (bars[-1]["c"] if bars else 0))
        naked_pocs_out = [
            {
                "price":        n.price,
                "session":      n.session,
                "age_sessions": n.age_sessions,
                "period_key":   n.period_key,
                "distance":     round(n.distance, 4),
            }
            for n in _get_npoc(symbol, _ref_price)
        ]
    except Exception:
        pass

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

    # Open positions — merge M1 + M2 with `mode` tag
    _FIB = (1, 1, 2, 3, 5)
    _base_lots_cfg = (settings.get("execution", {}).get("default_lots") or {})
    base_lot = float(_base_lots_cfg.get(symbol, 0.01))

    def _serialize_open(p, mode: str) -> dict:
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
        return {
            "position_id": p.position_id,
            "symbol":      p.symbol,
            "side":        p.side,
            "mode":        mode,
            "source":      p.source or mode,
            "avg_entry":   round(p.avg_entry, 4),
            "take_profit": round(p.take_profit, 4),
            "stop_loss":   round(p.stop_loss, 4),
            "legs_filled": p.leg_count,
            "leg_prices":  leg_prices,
            "legs":        legs_detail,
            "total_lots":  round(sum(l["lots"] for l in legs_detail), 4),
            "opened_ts":   p.opened_ts,
        }

    def _serialize_closed(p, mode: str) -> dict:
        leg_prices = [leg.entry for leg in p.legs]
        last_leg = p.legs[-1] if p.legs else None
        reason = (p.close_reason or "").lower()
        if "tp" in reason and last_leg:
            exit_price = last_leg.take_profit
        elif ("sl" in reason or "stop" in reason) and last_leg:
            exit_price = last_leg.stop_loss
        else:
            exit_price = last_leg.entry if last_leg else p.avg_entry
        return {
            "position_id": p.position_id,
            "side":         p.side,
            "mode":         mode,
            "source":       p.source or mode,
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
        }

    positions_out = []
    for p in position_store().open_positions():
        if p.symbol == symbol:
            positions_out.append(_serialize_open(p, "m1"))
    try:
        for p in position_store_m2().open_positions():
            if p.symbol == symbol:
                positions_out.append(_serialize_open(p, "m2"))
    except Exception as e:
        LOG.debug(f"[dashboard] m2 open_positions failed: {e}")

    closed_out: list[dict] = []
    try:
        for p in position_store().closed_positions(symbol, n=50):
            closed_out.append(_serialize_closed(p, "m1"))
        for p in position_store_m2().closed_positions(symbol, n=50):
            closed_out.append(_serialize_closed(p, "m2"))
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
    cvd_divergences: list[dict] = []
    try:
        from pipeline.features.cvd_candlestick import (
            build_cvd_candles, detect as detect_cvd, scan_divergences,
        )
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
        cvd_divergences = scan_divergences(raw_bars, lookback=3)
    except Exception as e:
        LOG.debug(f"[dashboard] cvd_candles failed: {e}")

    # Reversal-pattern markers (climax pivot → next-bar delta flip; diagnostic)
    reversal_patterns: list[dict] = []
    try:
        from pipeline.features.reversal_pattern import detect as detect_rev
        reversal_patterns = detect_rev(raw_bars, vol_mult=2.0, delta_swing=50.0,
                                       symbol=symbol, tf=tf, vp_filter=True)
    except Exception as e:
        LOG.debug(f"[dashboard] reversal_patterns failed: {e}")

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

    # Historical sweeps — read from TF-specific JSONL log, filter to chart window
    historical_sweeps: list[dict] = []
    try:
        from pipeline.features.sweep import read_sweep_log as _read_sweep_log
        _since = bars[0]["ts"] if bars else 0
        historical_sweeps = _read_sweep_log(symbol, since_ts=_since, tf=tf)
        for _s in historical_sweeps:
            _s.setdefault("source", "level")
    except Exception:
        pass

    # Swing points — up to 8 most-recent structural pivots per side
    # TF-aware lookback; EQH/EQL flagged for chart highlighting
    swing_points: list[dict] = []
    try:
        from pipeline.features.wave import detect_swing_points as _dsp
        if raw_bars:
            _lb_map = {"1m": 3, "3m": 3, "5m": 4, "15m": 5, "30m": 6, "1h": 7}
            _lookback = _lb_map.get(tf, 3)
            _sh, _sl = _dsp(raw_bars, lookback=_lookback)
            # Most-recent 8 per side (detect_swing_points returns ascending index order)
            _sh8 = list(reversed(_sh))[:8]
            _sl8 = list(reversed(_sl))[:8]
            _EQL_TOL = 0.0015
            def _is_eq(price, prices):
                return any(
                    abs(price - p) / max(p, 0.01) <= _EQL_TOL
                    for p in prices if abs(p - price) > 1e-9
                )
            _sh_px = [p for _, p in _sh8]
            _sl_px = [p for _, p in _sl8]
            for _idx, _price in _sh8:
                if 0 <= _idx < len(raw_bars):
                    swing_points.append({
                        "ts": raw_bars[_idx].close_ts,
                        "price": round(_price, 4),
                        "side": "high",
                        "is_equal": _is_eq(_price, _sh_px),
                    })
            for _idx, _price in _sl8:
                if 0 <= _idx < len(raw_bars):
                    swing_points.append({
                        "ts": raw_bars[_idx].close_ts,
                        "price": round(_price, 4),
                        "side": "low",
                        "is_equal": _is_eq(_price, _sl_px),
                    })
    except Exception as _e:
        LOG.warning(f"[dashboard] swing_points failed: {_e}")

    # Swing-derived sweeps — sweeps OF the displayed same-TF swing highs/lows.
    # Appended alongside the level-based log sweeps (source="level").
    try:
        from pipeline.features.sweep import detect_swing_sweeps as _dss
        if raw_bars and swing_points:
            historical_sweeps = historical_sweeps + _dss(raw_bars, swing_points)
    except Exception as _e:
        LOG.warning(f"[dashboard] swing_sweeps failed: {_e}")

    return {
        "symbol": symbol,
        "tf": tf,
        "bars": bars,
        "daily_vp": daily_vp,
        "prior_vp": prior_vp,
        "naked_pocs": naked_pocs_out,
        "detections": detections,
        "latest_m1": latest_m1,
        "latest_m2": latest_m2,
        "positions": {"open": positions_out, "pending": pending_out, "closed": closed_out},
        "cvd": cvd_candles,
        "cvd_signal": cvd_signal,
        "cvd_divergences": cvd_divergences,
        "reversal_patterns": reversal_patterns,
        "zones": zones_out,
        "ab_stats": ab_stats,
        "historical_sweeps": historical_sweeps,
        "swing_points": swing_points,
    }


@bp.get("/dashboard/state")
def dashboard_state():
    settings = current_app.config["FB_SETTINGS"]
    symbol = request.args.get("symbol") or settings["instrument"]["symbol"]
    tf = request.args.get("tf") or "1m"
    minutes = max(5, min(int(request.args.get("minutes", 120)), 7200))
    include_fp = request.args.get("footprint") == "true"
    session_align = request.args.get("session") == "today"
    snap = _build_snapshot(settings, symbol, tf, minutes, include_fp, session_align)
    resp = jsonify(snap)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@bp.get("/dashboard/stream")
def dashboard_stream():
    """SSE endpoint — server-pushed snapshots. Replaces client polling.
    Query params identical to /dashboard/state.
    """
    from flask import Response, stream_with_context
    settings = current_app.config["FB_SETTINGS"]
    symbol = request.args.get("symbol") or settings["instrument"]["symbol"]
    tf = request.args.get("tf") or "1m"
    minutes = max(5, min(int(request.args.get("minutes", 120)), 7200))
    include_fp = request.args.get("footprint") == "true"
    session_align = request.args.get("session") == "today"
    interval = float(request.args.get("interval", 1.5))
    interval = max(0.5, min(interval, 10.0))

    @stream_with_context
    def _gen():
        import time as _time
        last_bar_ts = 0
        last_bt_count = 0
        last_pos_sig = ""
        # Initial snapshot on connect
        try:
            snap = _build_snapshot(settings, symbol, tf, minutes, include_fp, session_align)
            yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
            last_bar_ts = snap["bars"][-1]["ts"] if snap.get("bars") else 0
            last_bt_count = len(snap.get("detections", {}).get("big_trades", []))
            last_pos_sig = _pos_signature(snap.get("positions", {}))
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'msg': str(e)[:200]})}\n\n"
            return
        while True:
            _time.sleep(interval)
            try:
                snap = _build_snapshot(settings, symbol, tf, minutes, include_fp, session_align)
                cur_bar_ts = snap["bars"][-1]["ts"] if snap.get("bars") else 0
                cur_bt_count = len(snap.get("detections", {}).get("big_trades", []))
                cur_pos_sig = _pos_signature(snap.get("positions", {}))
                # Always emit "tick" with live bar; rest only when changed
                # to avoid client doing heavy rerender every cycle
                if (cur_bar_ts != last_bar_ts or cur_bt_count != last_bt_count
                        or cur_pos_sig != last_pos_sig):
                    yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
                    last_bar_ts, last_bt_count, last_pos_sig = cur_bar_ts, cur_bt_count, cur_pos_sig
                else:
                    # Send just the live bar so chart stays live without resending VP/bubbles/etc
                    live_only = {
                        "live_bar": snap["bars"][-1] if snap.get("bars") else None,
                        "ts": int(_time.time()),
                    }
                    yield f"event: tick\ndata: {json.dumps(live_only)}\n\n"
            except GeneratorExit:
                return
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'msg': str(e)[:200]})}\n\n"

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
        "Connection": "keep-alive",
    }
    return Response(_gen(), headers=headers)


def _pos_signature(pos: dict) -> str:
    """Cheap diff key for positions block. Changes when open/closed sets shift."""
    open_ids = sorted(p.get("position_id", "") for p in pos.get("open", []))
    closed_ids = sorted(p.get("position_id", "") for p in pos.get("closed", []))
    pending_ids = sorted(p.get("pending_id", "") for p in pos.get("pending", []))
    return f"{len(open_ids)}|{open_ids[-3:]}|{len(closed_ids)}|{closed_ids[-3:]}|{len(pending_ids)}|{pending_ids[-3:]}"


@bp.post("/dashboard/reload_stores")
def reload_stores():
    """Force-reload position_store and position_store_m2 from JSONL.

    Call after patching positions.jsonl / cycles.jsonl (e.g. bar_verify_outcomes.py)
    without restarting the server.
    """
    import execution.position_store as _ps_mod
    import threading

    with _ps_mod._store_lock:
        _ps_mod._store = None
        _ps_mod._store_m2 = None

    # Re-instantiate immediately
    _ps_mod.position_store()
    _ps_mod.position_store_m2()

    n_m1 = len(list(_ps_mod.position_store()._positions.values()))
    n_m2 = len(list(_ps_mod.position_store_m2()._positions.values()))
    LOG.info(f"[dashboard] stores reloaded: m1={n_m1} positions, m2={n_m2} positions")
    return jsonify({"ok": True, "m1_positions": n_m1, "m2_positions": n_m2})
