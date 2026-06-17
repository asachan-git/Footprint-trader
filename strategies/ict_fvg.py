"""IctFvg — ICT/SMC directional setup: 15m FVG rejection → 1m ChoCh → Fib premium
→ 1m FVG entry. PAPER ONLY (provisional, accumulating out-of-sample samples).

Mechanic (short example; inverts for long):
  1. HTF (15m) — an UNMITIGATED bearish FVG is resistance. Price rallies INTO it and
     the latest 15m bar shows a response/rejection (closes back in the lower half of
     the zone). That arms an HTF SHORT bias for the next few 15m bars.
  2. LTF (1m) — while the HTF bias is armed, wait for a 1m ChoCh-bear: price was making
     HH/HL into the FVG, then a 1m bar closes below the last Higher Low (structure flip).
  3. Fib — draw the impulse leg (swing high → impulse low) and take the PREMIUM zone
     between fib_lo (0.5) and fib_hi (0.786), focus 0.618 (golden).
  4. Confluence — a 1m BEARISH FVG created during the ChoCh move whose midpoint sits in
     the premium zone. ENTRY = a sell-limit at that 1m FVG's midpoint.
  5. SL — static, just OUTSIDE the FVG-producing wick (+ ATR buffer). TP — hard 4R, or
     (tp_mode=fvg) the next unmitigated 15m FVG midpoint in the trend direction.

Reuses Coup's execution plumbing (single tactical leg, structural-SL clamp, forced TP via
_pending_sl/_pending_tp) and reversal_choch's arm→fill _pending_entry pattern — the paper
executor fills leg-1 immediately, so the limit-touch gate MUST live here (emit the Decision
only on the 1m bar that straddles the FVG midpoint).

Exit policy (settings_override + cycle_manager):
  - hard_sl_exit ON  → the static structural SL closes the cycle.
  - be_at_r          → move SL→entry at +1R; this APPROXIMATES the spec's "close beyond the
                       ChoCh swing low → break-even" (the swing low is ≈1R for these setups).
  - tp_mutation_enabled OFF → poc_trail / tp_participation don't rewrite the hard 4R TP.
  - hedge_eval / coup_flip / cvd off.

Deferred (framework can't express cleanly — see plan): candle-by-candle 8R-20R trail,
50%-at-2R/3R partials, truly-dynamic 15m-FVG TP re-targeting. own store: data/strategies/<name>/.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.choch import detect_choch, impulse_leg
from pipeline.features.fvg import detect_fvgs, nearest_fvg_above, nearest_fvg_below

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)


class IctFvg(Coup):
    name = "ict_fvg"

    _TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("symbols", ["BTCUSDT"])
        cfg.setdefault("decide_tf", "1m")          # entry state-machine + 1m ChoCh run on 1m
        cfg.setdefault("htf_tf", "15m")            # FVG context pulled inside decide()
        cfg.setdefault("htf_fvg_max_age", 100)
        cfg.setdefault("htf_arm_bars", 4)          # HTF bias valid this many 15m bars
        cfg.setdefault("swing_n", 2)
        cfg.setdefault("choch_lookback", 200)
        cfg.setdefault("arm_within", 10)           # 1m ChoCh break must be this fresh
        cfg.setdefault("ltf_fvg_max_age", 50)
        cfg.setdefault("fib_lo", 0.5)
        cfg.setdefault("fib_hi", 0.786)
        cfg.setdefault("fib_focus", 0.618)
        cfg.setdefault("entry_expiry_bars", 30)    # 1m bars the FVG-mid limit waits for touch
        cfg.setdefault("sl_buf_atr", 0.10)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        cfg.setdefault("tp_mode", "r")             # "r" = hard tp_r·R | "fvg" = next 15m FVG mid
        cfg.setdefault("tp_r", 4.0)
        cfg.setdefault("min_rr", 1.5)              # fvg-target must clear this RR else fall to R
        cfg.setdefault("be_at_r", 1.0)             # break-even trigger (≈ ChoCh-swing-low)
        super().__init__(cfg)
        self._pending_entry: dict[str, dict] = {}
        self._htf_armed: dict[str, dict] = {}
        self._seen_choch: dict[str, int] = {}

    # ── execution policy ─────────────────────────────────────────────────────
    def settings_override(self, settings: dict) -> dict:
        cfg = self.config
        cyc = {**(settings.get("cycle") or {}),
               "hard_sl_exit": True,
               "hedge_eval_enabled": False,
               "coup_flip_exit": False,
               "cvd_divergence_exit": False,
               "tp_mutation_enabled": False,        # hold the hard 4R TP (no poc_trail/tp_part drift)
               "be_at_r": float(cfg.get("be_at_r", 1.0))}
        return {**settings, "cycle": cyc}

    # ── HTF (15m) — unmitigated FVG retrace + rejection arms a directional bias ──
    def _htf_trigger(self, symbol: str) -> dict | None:
        cfg = self.config
        htf_tf = str(cfg.get("htf_tf", "15m"))
        hb = store().recent(symbol, htf_tf, int(cfg.get("htf_fvg_max_age", 100)) + 5)
        if len(hb) < 5:
            return None
        fvgs = detect_fvgs(hb, max_age_bars=int(cfg.get("htf_fvg_max_age", 100)))
        last = hb[-1]
        c, h, l = last.ohlc.c, last.ohlc.h, last.ohlc.l
        best = None   # (dist_to_close, side, fvg)
        for f in fvgs:
            rng = f.high - f.low
            if rng <= 0 or not (h >= f.low and l <= f.high):   # must overlap the zone
                continue
            mid = f.low + 0.5 * rng
            if f.side == "bear" and c < mid:                   # rallied into resistance, rejected
                side = "short"
            elif f.side == "bull" and c > mid:                 # dropped into support, rejected
                side = "long"
            else:
                continue
            d = abs(mid - c)
            if best is None or d < best[0]:
                best = (d, side, f)
        if best is None:
            return None
        _, side, f = best
        return {"side": side, "fvg": f, "ts": last.close_ts,
                "expiry_ts": last.close_ts + int(cfg.get("htf_arm_bars", 4)) * self._TF_SEC.get(htf_tf, 900)}

    # ── LTF (1m) — ChoCh + Fib premium + 1m FVG confluence → arm the limit ──
    def _arm_signal(self, symbol: str, ltf: list[Bar], ht: dict) -> dict | None:
        cfg = self.config
        n = int(cfg.get("swing_n", 2))
        lookback = int(cfg.get("choch_lookback", 200))
        side = ht["side"]
        want_dir = "bear" if side == "short" else "bull"
        want_fvg = "bear" if side == "short" else "bull"
        win = ltf[-lookback:] if len(ltf) > lookback else ltf

        event = detect_choch(win, n=n, lookback_bars=lookback)
        if event is None or event.direction != want_dir:
            return None
        if self._seen_choch.get(symbol) == event.broken_at_ts:
            return None
        leg = impulse_leg(win, event, n=n)
        if leg is None:
            self._seen_choch[symbol] = event.broken_at_ts
            return None
        origin, extreme, brk_idx = leg
        if brk_idx < len(win) - int(cfg.get("arm_within", 10)):   # stale break
            self._seen_choch[symbol] = event.broken_at_ts
            return None
        span = (origin - extreme) if side == "short" else (extreme - origin)
        if span <= 0:
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        fib_lo, fib_hi = float(cfg.get("fib_lo", 0.5)), float(cfg.get("fib_hi", 0.786))
        focus = float(cfg.get("fib_focus", 0.618))
        if side == "short":
            band_lo, band_hi, focus_p = extreme + fib_lo * span, extreme + fib_hi * span, extreme + focus * span
        else:
            band_lo, band_hi, focus_p = extreme - fib_hi * span, extreme - fib_lo * span, extreme - focus * span

        fvgs = [f for f in detect_fvgs(win, max_age_bars=int(cfg.get("ltf_fvg_max_age", 50)))
                if f.side == want_fvg and band_lo <= f.mid <= band_hi]
        if not fvgs:
            self._seen_choch[symbol] = event.broken_at_ts
            return None
        chosen = min(fvgs, key=lambda f: abs(f.mid - focus_p))
        entry = float(chosen.mid)
        last = ltf[-1]
        # the limit must sit on the correct side of current price (await the retrace touch)
        if (side == "short" and entry <= last.ohlc.c) or (side == "long" and entry >= last.ohlc.c):
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        a = atr(win) or 0.0
        buf = float(cfg.get("sl_buf_atr", 0.10)) * a
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * a, 1e-9)
        if side == "short":
            sl = max(chosen.high + buf, entry + min_dist)   # outside the FVG wick + buffer
            risk = sl - entry
        else:
            sl = min(chosen.low - buf, entry - min_dist)
            risk = entry - sl
        if risk <= 0:
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        tp_r = float(cfg.get("tp_r", 4.0))
        tp = entry - tp_r * risk if side == "short" else entry + tp_r * risk
        tp_src = "r"
        if str(cfg.get("tp_mode", "r")) == "fvg":
            hb = store().recent(symbol, str(cfg.get("htf_tf", "15m")),
                                int(cfg.get("htf_fvg_max_age", 100)) + 5)
            hf = detect_fvgs(hb, max_age_bars=int(cfg.get("htf_fvg_max_age", 100)))
            tgt = (nearest_fvg_below(hf, entry, "bear") if side == "short"
                   else nearest_fvg_above(hf, entry, "bull"))
            if tgt is not None:
                ct = tgt.mid
                ok = (ct < entry) if side == "short" else (ct > entry)
                if ok and abs(ct - entry) / risk >= float(cfg.get("min_rr", 1.5)):
                    tp, tp_src = float(ct), "fvg"

        if (side == "short" and not (tp < entry < sl)) or (side == "long" and not (sl < entry < tp)):
            self._seen_choch[symbol] = event.broken_at_ts
            return None

        self._seen_choch[symbol] = event.broken_at_ts
        return {"ts": event.broken_at_ts, "side": side, "entry": round(entry, 4),
                "sl": round(sl, 4), "tp": round(tp, 4), "origin": round(origin, 4),
                "extreme": round(extreme, 4), "tp_src": tp_src, "choch_dir": event.direction,
                "fvg_low": round(chosen.low, 4), "fvg_high": round(chosen.high, 4)}

    def _build_decision(self, symbol: str, m: dict, entry: float) -> Decision:
        sl, tp = float(m["sl"]), float(m["tp"])
        self._acted[symbol] = m["ts"]
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp
        risk = abs(entry - sl) or 1e-9
        rr = abs(tp - entry) / risk
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} ICT @{entry:.2f} "
                 f"FVG[{m['fvg_low']:.2f},{m['fvg_high']:.2f}] SL={sl:.2f} TP={tp:.2f}"
                 f"({m['tp_src']}) RR={rr:.2f}")
        return Decision(
            side=m["side"], entry=entry, stop_loss=sl, take_profit=tp,
            confidence=_clamp(0.45 + 0.04 * (rr - 1.0), 0.0, 0.9), bias_strength=3,
            rationale=(
                f"ict_fvg: {m['side']} — 15m FVG rejection armed the bias; 1m "
                f"{m['choch_dir']} ChoCh (impulse {m['origin']:.2f}→{m['extreme']:.2f}); "
                f"sell-limit at the premium-zone 1m FVG mid {entry:.2f}; SL outside the "
                f"FVG wick {sl:.2f}; TP {tp:.2f} ({m['tp_src']}, {rr:.1f}R)."
            ),
            invalidation_note="price closes back through the FVG / structural SL; BE at +1R",
        )

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        n = int(cfg.get("swing_n", 2))
        ltf = store().recent(symbol, "1m", int(cfg.get("choch_lookback", 200)) + 10)
        if len(ltf) < 2 * n + 6:
            return None
        last = ltf[-1]

        # ── 1. resolve a pending FVG-mid LIMIT: fill on touch, else expire ──
        pe = self._pending_entry.get(symbol)
        if pe is not None:
            if last.close_ts > pe["expiry_ts"] or self._acted.get(symbol) == pe["m"]["ts"]:
                self._pending_entry.pop(symbol, None)
            elif last.ohlc.l <= pe["level"] <= last.ohlc.h:
                self._pending_entry.pop(symbol, None)
                return self._build_decision(symbol, pe["m"], pe["level"])
            else:
                return None

        # ── 2. HTF (15m) bias — unmitigated FVG retrace + rejection ──
        ht = self._htf_trigger(symbol)
        if ht is None or last.close_ts > ht["expiry_ts"]:
            return None
        self._htf_armed[symbol] = ht

        # ── 3. LTF (1m) ChoCh + Fib + 1m FVG confluence → arm the limit ──
        m = self._arm_signal(symbol, ltf, ht)
        if m is None:
            return None
        self._pending_entry[symbol] = {
            "m": m, "level": float(m["entry"]),
            "expiry_ts": last.close_ts + int(cfg.get("entry_expiry_bars", 30)) * self._TF_SEC.get("1m", 60),
        }
        LOG.info(f"[{self.name}] {symbol} {m['side'].upper()} armed — LIMIT @{m['entry']:.2f} "
                 f"(premium-zone 1m FVG), waiting ≤{cfg.get('entry_expiry_bars', 30)} 1m bars for touch")
        return None
