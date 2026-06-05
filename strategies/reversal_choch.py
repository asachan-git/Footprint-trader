"""ReversalChoch — structure-flip (ChoCh) reversal with Fibonacci retrace entry / extension TP.

Replaces the old climax-pivot `reversal` (which ran ~breakeven and net-negative in
paper). The mechanic is market-structure (SMC/ICT) rather than footprint-climax:

  1. WAIT — while 15m structure holds its trend (HH/HL or LL/LH), do nothing. This is
     the "continuation phase": no trades, just track structure. (Per design: wait-only,
     no continuation entries — we only act once character changes.)
  2. ChoCh — when price breaks the last HL (downward) or last LH (upward), the trend's
     character has changed (pipeline.features.choch.detect_choch). A bull ChoCh arms a
     LONG reversal; a bear ChoCh a SHORT.
  3. ENTRY — a LIMIT at the `fib_entry` retracement of the impulse leg that broke
     structure (origin swing → impulse extreme). We wait ≤ entry_expiry_bars for price
     to retrace into it; else the signal voids.
  4. TP / SL — TP at the `fib_ext` extension of that same leg (projected past the
     extreme); SL just beyond the swing ORIGIN (the 0.0 anchor) — invalidates the leg.

Fib levels are config-driven so the three live variants A/B them:
  0.618 entry / 1.618 ext  (classic),  0.705 / 1.272 (ICT OTE),  0.5 / 2.0 (deep TP).

Reuses Coup's execution plumbing (single tactical leg, structural SL clamp, forced TP
via _pending_sl/_pending_tp) — only `decide` and the exit policy differ. Own store:
data/strategies/<name>/. PROVISIONAL — paper only, accumulating out-of-sample samples.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.choch import detect_choch, impulse_leg
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)

# VP window per tf for the VP-level-target mode (trailing volume profile).
VP_WIN = {"1m": 1440, "5m": 288, "15m": 96}


class ReversalChoch(Coup):
    name = "reversal_choch"

    _TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("symbols", ["BTCUSDT"])
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("swing_n", 2)            # 5-bar fractal for structure swings
        cfg.setdefault("choch_lookback", 200)   # bars scanned for the latest ChoCh
        cfg.setdefault("arm_within", 10)        # only arm if the break is this fresh
        cfg.setdefault("fib_entry", 0.618)      # retrace fill level on the impulse leg
        cfg.setdefault("fib_ext", 1.618)        # extension TP projected past the extreme
        cfg.setdefault("entry_expiry_bars", 6)  # bars a LIMIT waits for the retrace touch
        cfg.setdefault("sl_buf_atr", 0.10)      # buffer beyond the swing origin (× ATR)
        cfg.setdefault("min_sl_atr_mult", 0.5)  # floor on entry↔SL distance
        cfg.setdefault("tp_mode", "fib")        # "fib" = fib_ext extension; "vp" = nearest VP level beyond the extreme
        cfg.setdefault("vp_tp_min_rr", 1.0)     # vp-target must clear this RR, else fall back to fib_ext
        super().__init__(cfg)
        self._pending_entry: dict[str, dict] = {}   # symbol → armed LIMIT awaiting touch
        self._seen_choch: dict[str, int] = {}       # symbol → broken_at_ts already handled

    # ── exits: structural only. No absorption-flip (footprint mechanic, not ours);
    #    let the Fib extension TP / origin SL run. CVD-divergence opt-in. ──
    def settings_override(self, settings: dict) -> dict:
        cfg = self.config
        cyc = {**(settings.get("cycle") or {}),
               "hard_sl_exit": True,
               "hedge_eval_enabled": False,
               "coup_flip_exit": False,
               "cvd_divergence_exit": bool(cfg.get("cvd_divergence_exit", False)),
               "cvd_exit_conf": float(cfg.get("cvd_exit_conf", 0.65))}
        return {**settings, "cycle": cyc}

    def _build_decision(self, symbol: str, m: dict, entry: float) -> Decision:
        sl, tp = float(m["sl"]), float(m["tp"])
        self._acted[symbol] = m["ts"]
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} ChoCh fade @{entry:.2f} "
                 f"fib{m['fib_entry']}→{m['fib_ext']} origin={m['origin']:.2f} "
                 f"ext={m['extreme']:.2f} SL={sl:.2f} TP={tp:.2f} (RR {rr:.2f})")
        return Decision(
            side=m["side"], entry=entry, stop_loss=sl, take_profit=tp,
            confidence=_clamp(0.45 + 0.05 * (rr - 1.0), 0.0, 0.9), bias_strength=3,
            rationale=(
                f"reversal_choch: {m['side']} after a 15m {m['choch_dir']} ChoCh "
                f"(broke {m['broken_level']:.2f}, prior trend {m['last_trend']}). "
                f"Impulse leg {m['origin']:.2f}→{m['extreme']:.2f}; entry at "
                f"{m['fib_entry']} retrace ({entry:.2f}), SL beyond origin {sl:.2f}, "
                f"TP at {m['fib_ext']} extension {tp:.2f}."
            ),
            invalidation_note="price closes past the swing origin (leg invalidated) → structural SL",
        )

    def _vp_tp(self, symbol: str, bars: list[Bar], decide_tf: str,
               side: str, extreme: float) -> float | None:
        """VP-level target: nearest volume-profile level (POC / VA edge / naked POC /
        HVN-zone midpoint) BEYOND the impulse extreme in the trade direction. The fade
        runs in the new ChoCh direction, so a long aims at the first VP level above the
        break high, a short at the first below the break low. None → caller keeps fib TP."""
        win_n = VP_WIN.get(decide_tf, 96)
        seg = bars[-win_n:] if len(bars) > win_n else bars
        if len(seg) < 20:
            return None
        try:
            vp = vp_compute(seg, "intraday", bars[-1].ohlc.c,
                            bin_size=DEFAULT_BIN_SIZE.get(symbol))
        except Exception:
            return None
        levels: list[float] = []
        for k in (vp.poc, vp.vah, vp.val, vp.naked_poc):
            if k is not None:
                levels.append(float(k))
        for z in (vp.hvn_zones or []):
            levels.append((float(z["low"]) + float(z["high"])) / 2.0)
        cand = [L for L in levels if (L > extreme) if side == "long"] if side == "long" \
            else [L for L in levels if L < extreme]
        if not cand:
            return None
        return min(cand) if side == "long" else max(cand)   # nearest beyond the extreme

    def _arm_signal(self, symbol: str, bars: list[Bar], decide_tf: str) -> dict | None:
        """Detect a fresh 15m ChoCh and build the Fib retrace/extension levels."""
        cfg = self.config
        n = int(cfg.get("swing_n", 2))
        lookback = int(cfg.get("choch_lookback", 200))
        win = bars[-lookback:] if len(bars) > lookback else bars
        event = detect_choch(win, n=n, lookback_bars=lookback)
        if event is None:
            return None
        if self._seen_choch.get(symbol) == event.broken_at_ts:
            return None                                   # already armed/acted this ChoCh
        leg = impulse_leg(win, event, n=n)
        if leg is None:
            return None
        origin, extreme, brk_idx = leg
        # freshness: only arm if the structure break is recent (else the retrace has
        # likely already played out and we'd chase).
        if brk_idx < len(win) - int(cfg.get("arm_within", 10)):
            self._seen_choch[symbol] = event.broken_at_ts   # mark stale → don't re-scan
            return None

        side = "long" if event.direction == "bull" else "short"
        span = (extreme - origin) if side == "long" else (origin - extreme)
        if span <= 0:
            return None
        fib_entry = float(cfg.get("fib_entry", 0.618))
        fib_ext = float(cfg.get("fib_ext", 1.618))
        if side == "long":
            entry = extreme - fib_entry * span
            sl_raw = origin
            tp = extreme + (fib_ext - 1.0) * span
        else:
            entry = extreme + fib_entry * span
            sl_raw = origin
            tp = extreme - (fib_ext - 1.0) * span

        last = bars[-1]
        # must be a genuine pullback from current price (long below / short above)
        if (side == "long" and entry >= last.ohlc.c) or (side == "short" and entry <= last.ohlc.c):
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        a = atr(bars) or 0.0
        buf = float(cfg.get("sl_buf_atr", 0.10)) * a
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * a, 1e-9)
        if side == "long":
            sl = min(sl_raw - buf, entry - min_dist)
        else:
            sl = max(sl_raw + buf, entry + min_dist)

        # TP source: fib_ext extension (default) or nearest VP level beyond the extreme.
        # VP target must run the right way and clear vp_tp_min_rr, else keep the fib TP.
        tp_src = "fib"
        if str(cfg.get("tp_mode", "fib")) == "vp":
            vp_tp = self._vp_tp(symbol, bars, decide_tf, side, extreme)
            if vp_tp is not None:
                risk = abs(entry - sl) or 1e-9
                ok_dir = (vp_tp > entry) if side == "long" else (vp_tp < entry)
                if ok_dir and abs(vp_tp - entry) / risk >= float(cfg.get("vp_tp_min_rr", 1.0)):
                    tp, tp_src = float(vp_tp), "vp"

        if (side == "long" and not (sl < entry < tp)) or (side == "short" and not (tp < entry < sl)):
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        self._seen_choch[symbol] = event.broken_at_ts
        return {
            "ts": event.broken_at_ts, "side": side, "entry": round(entry, 4),
            "sl": round(sl, 4), "tp": round(tp, 4), "origin": round(origin, 4),
            "extreme": round(extreme, 4), "fib_entry": fib_entry, "fib_ext": fib_ext,
            "tp_src": tp_src,
            "choch_dir": event.direction, "last_trend": event.last_trend,
            "broken_level": round(event.broken_level, 4),
        }

    @classmethod
    def scan(cls, bars: list[Bar], symbol: str, tf: str,
             config: dict | None = None, warmup: int = 40) -> list[dict]:
        """Diagnostic: every ChoCh setup this strategy WOULD arm across `bars`, for a
        chart overlay. Walks bars[:i+1] through a fresh instance's _arm_signal (same
        detect + Fib + dedup as live); tags each with `arm_ts` = the arming bar."""
        s = cls(config={**(config or {}), "decide_tf": tf})
        out: list[dict] = []
        for i in range(warmup, len(bars)):
            m = s._arm_signal(symbol, bars[:i + 1], tf)
            if m:
                out.append({**m, "arm_ts": bars[i].close_ts})
        return out

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        need = int(cfg.get("choch_lookback", 200)) + 10
        bars = store().recent(symbol, decide_tf, need)
        if len(bars) < 2 * int(cfg.get("swing_n", 2)) + 6:
            return None
        last = bars[-1]
        tf_sec = self._TF_SEC.get(decide_tf, 900)

        # ── 1. resolve a pending LIMIT: fill on retrace touch, else expire ──
        pe = self._pending_entry.get(symbol)
        if pe is not None:
            if last.close_ts > pe["expiry_ts"] or self._acted.get(symbol) == pe["m"]["ts"]:
                self._pending_entry.pop(symbol, None)
            elif last.ohlc.l <= pe["level"] <= last.ohlc.h:        # touched → enter
                self._pending_entry.pop(symbol, None)
                return self._build_decision(symbol, pe["m"], pe["level"])
            else:
                return None                                        # still waiting

        # ── 2. detect a fresh ChoCh and arm the Fib retrace LIMIT ──
        m = self._arm_signal(symbol, bars, decide_tf)
        if m is None:
            return None
        self._pending_entry[symbol] = {
            "m": m, "level": float(m["entry"]),
            "expiry_ts": last.close_ts + int(cfg.get("entry_expiry_bars", 6)) * tf_sec,
        }
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} ChoCh armed — LIMIT "
                 f"@{m['entry']:.2f} (fib {m['fib_entry']}), waiting "
                 f"≤{cfg.get('entry_expiry_bars', 6)} bars for retrace touch")
        return None
