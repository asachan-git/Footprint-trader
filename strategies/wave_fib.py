"""WaveFib — trend-CONTINUATION Fibonacci entry off a confirmed two-wave structure.

The with-trend complement to reversal_choch (which fades a structure break). Here we
WAIT for a trend to confirm itself in two waves, then join the THIRD wave:

  - Bull: a Higher-High (wave 1: L0→H1) then a Higher-Low (wave 2 pullback: H1→L1).
    HH+HL confirms an uptrend → arm a LONG for the 3rd wave up.
  - Short: a Lower-Low then a Lower-High (LL+LH) → arm a SHORT for the 3rd wave down.

ENTRY (limit, into the wave-2 pullback zone, ≤ entry_expiry_bars to fill, else void):
  - entry_mode=vp   — a Volume-Profile level of the two-wave structure (POC by default;
    val/vah for the discount/premium edge). The price the structure transacted fair
    value at = where the pullback tends to find support/resistance.  [primary]
  - entry_mode=fib  — the fib_entry retrace of wave 1 (impulse extreme → origin).
  Either way the level is clamped to sit between the pullback pivot and the impulse
  extreme; outside that → fall back (vp→fib→midpoint).

TP   = fib_ext × wave-1 length projected from the pullback pivot (measured move; 1.0 =
       1:1, 1.618 = extension). SL = just beyond the pullback pivot (HL/LH) — if that
       breaks, the higher-low / lower-high structure is invalidated.

Reuses Coup's single-leg execution plumbing. Own store per instance. PROVISIONAL — paper.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.choch import continuation_leg
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)


class WaveFib(Coup):
    name = "wave_fib"

    _TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("symbols", ["BTCUSDT"])
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("swing_n", 2)
        cfg.setdefault("struct_lookback", 200)
        cfg.setdefault("arm_within", 8)          # bars since the pullback pivot to stay fresh
        cfg.setdefault("entry_mode", "vp")       # vp | fib
        cfg.setdefault("vp_level", "poc")        # poc | value (val for long / vah for short)
        cfg.setdefault("fib_entry", 0.5)         # entry_mode=fib: retrace of wave 1
        cfg.setdefault("fib_ext", 1.0)           # TP = fib_ext × wave-1 length from pullback
        cfg.setdefault("entry_expiry_bars", 6)
        cfg.setdefault("sl_buf_atr", 0.10)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        super().__init__(cfg)
        self._pending_entry: dict[str, dict] = {}
        self._seen: dict[str, int] = {}          # symbol → pullback_idx-derived key acted

    def settings_override(self, settings: dict) -> dict:
        cfg = self.config
        cyc = {**(settings.get("cycle") or {}),
               "hard_sl_exit": True,
               "hedge_eval_enabled": False,
               "coup_flip_exit": False,
               "cvd_divergence_exit": bool(cfg.get("cvd_divergence_exit", False)),
               "cvd_exit_conf": float(cfg.get("cvd_exit_conf", 0.65))}
        return {**settings, "cycle": cyc}

    # ── VP entry level over the two-wave structure ──────────────────────────────
    def _vp_entry(self, symbol: str, bars: list[Bar], st) -> float | None:
        seg = bars[st.origin_idx:st.pullback_idx + 1]
        if len(seg) < 3:
            return None
        vp = vp_compute(seg, "intraday", bars[-1].ohlc.c,
                        bin_size=DEFAULT_BIN_SIZE.get(symbol))
        if str(self.config.get("vp_level", "poc")) == "value":
            lvl = vp.val if st.side == "long" else vp.vah
        else:
            lvl = vp.poc
        return float(lvl) if lvl is not None else None

    def _build_decision(self, symbol: str, m: dict, entry: float) -> Decision:
        sl, tp = float(m["sl"]), float(m["tp"])
        self._acted[symbol] = m["ts"]
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} 3rd-wave @{entry:.2f} "
                 f"({m['entry_kind']}) wave1={m['origin']:.2f}->{m['impulse']:.2f} "
                 f"HL/LH={m['pullback']:.2f} SL={sl:.2f} TP={tp:.2f} (RR {rr:.2f})")
        return Decision(
            side=m["side"], entry=entry, stop_loss=sl, take_profit=tp,
            confidence=_clamp(0.45 + 0.05 * (rr - 1.0), 0.0, 0.9), bias_strength=3,
            rationale=(
                f"wave_fib: {m['side']} continuation — confirmed two-wave structure "
                f"(wave-1 {m['origin']:.2f}→{m['impulse']:.2f}, pullback pivot "
                f"{m['pullback']:.2f}); 3rd-wave entry[{m['entry_kind']}] {entry:.2f}, "
                f"SL beyond the {'HL' if m['side']=='long' else 'LH'} {sl:.2f}, "
                f"TP {m['fib_ext']}× measured move {tp:.2f}."
            ),
            invalidation_note="price breaks the pullback pivot (HL/LH) → structure invalid → SL",
        )

    def _arm_signal(self, symbol: str, bars: list[Bar]) -> dict | None:
        cfg = self.config
        n = int(cfg.get("swing_n", 2))
        lookback = int(cfg.get("struct_lookback", 200))
        win = bars[-lookback:] if len(bars) > lookback else bars
        st = continuation_leg(win, n=n)
        if st is None:
            return None
        # one attempt per structure: key by the pullback pivot's bar
        key = win[st.pullback_idx].close_ts
        if self._seen.get(symbol) == key:
            return None
        # freshness: pullback pivot must be recent (else the 3rd wave likely already ran)
        if st.pullback_idx < len(win) - int(cfg.get("arm_within", 8)):
            self._seen[symbol] = key
            return None
        self._seen[symbol] = key

        side = st.side
        span = (st.impulse - st.origin) if side == "long" else (st.origin - st.impulse)
        if span <= 0:
            return None
        last = win[-1]
        fib_ext = float(cfg.get("fib_ext", 1.0))
        lo, hi = (st.pullback, st.impulse) if side == "long" else (st.impulse, st.pullback)

        # ── entry level ──
        entry = None
        kind = str(cfg.get("entry_mode", "vp"))
        if kind == "vp":
            entry = self._vp_entry(symbol, win, st)
            entry_kind = f"vp_{cfg.get('vp_level','poc')}"
        if entry is None or not (lo < entry < hi):     # fib fallback / fib mode
            fib_entry = float(cfg.get("fib_entry", 0.5))
            entry = (st.impulse - fib_entry * span) if side == "long" else (st.impulse + fib_entry * span)
            entry_kind = f"fib_{fib_entry}"
        if not (lo < entry < hi):                       # last resort: pullback↔impulse mid
            entry = (lo + hi) / 2
            entry_kind = "mid"

        # must be a genuine pullback from current price (long below / short above)
        if (side == "long" and entry >= last.ohlc.c) or (side == "short" and entry <= last.ohlc.c):
            return None

        a = atr(bars) or 0.0
        buf = float(cfg.get("sl_buf_atr", 0.10)) * a
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * a, 1e-9)
        if side == "long":
            sl = min(st.pullback - buf, entry - min_dist)
            tp = st.pullback + fib_ext * span
        else:
            sl = max(st.pullback + buf, entry + min_dist)
            tp = st.pullback - fib_ext * span
        if (side == "long" and not (sl < entry < tp)) or (side == "short" and not (tp < entry < sl)):
            return None

        return {
            "ts": key, "side": side, "entry": round(entry, 4), "entry_kind": entry_kind,
            "sl": round(sl, 4), "tp": round(tp, 4), "origin": round(st.origin, 4),
            "impulse": round(st.impulse, 4), "pullback": round(st.pullback, 4),
            "fib_ext": fib_ext,
        }

    @classmethod
    def scan(cls, bars: list[Bar], symbol: str, tf: str,
             config: dict | None = None, warmup: int = 40) -> list[dict]:
        """Diagnostic: every two-wave continuation setup this strategy WOULD arm across
        `bars`, for a chart overlay. Walks bars[:i+1] through a fresh instance's
        _arm_signal (same structure + entry + dedup as live); tags `arm_ts`."""
        s = cls(config={**(config or {}), "decide_tf": tf})
        out: list[dict] = []
        for i in range(warmup, len(bars)):
            m = s._arm_signal(symbol, bars[:i + 1])
            if m:
                out.append({**m, "arm_ts": bars[i].close_ts})
        return out

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        need = int(cfg.get("struct_lookback", 200)) + 10
        bars = store().recent(symbol, decide_tf, need)
        if len(bars) < 2 * int(cfg.get("swing_n", 2)) + 6:
            return None
        last = bars[-1]
        tf_sec = self._TF_SEC.get(decide_tf, 900)

        # ── 1. resolve a pending LIMIT ──
        pe = self._pending_entry.get(symbol)
        if pe is not None:
            if last.close_ts > pe["expiry_ts"] or self._acted.get(symbol) == pe["m"]["ts"]:
                self._pending_entry.pop(symbol, None)
            elif last.ohlc.l <= pe["level"] <= last.ohlc.h:
                self._pending_entry.pop(symbol, None)
                return self._build_decision(symbol, pe["m"], pe["level"])
            else:
                return None

        # ── 2. detect a fresh two-wave structure and arm the entry ──
        m = self._arm_signal(symbol, bars)
        if m is None:
            return None
        self._pending_entry[symbol] = {
            "m": m, "level": float(m["entry"]),
            "expiry_ts": last.close_ts + int(cfg.get("entry_expiry_bars", 6)) * tf_sec,
        }
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} structure armed — LIMIT "
                 f"@{m['entry']:.2f} ({m['entry_kind']}), waiting "
                 f"≤{cfg.get('entry_expiry_bars', 6)} bars for the pullback touch")
        return None
