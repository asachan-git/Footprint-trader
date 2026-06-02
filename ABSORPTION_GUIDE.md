# Absorption Candle Identification Guide

How to spot the candles `coup` (momentum) and `coup_reversal` (reversal) fire on,
both mechanically (code rules) and by eye on a footprint chart.

Convention (whole repo): on the footprint ladder, **ask volume = aggressive BUYs**,
**bid volume = aggressive SELLs** (see `bybit/footprint_builder.py`). "Winner" =
the side we join: `side="buy"` → LONG, `side="sell"` → SHORT.

Detector: `pipeline/features/absorption.py :: detect_canonical_absorption(bar, fp, mode)`.
Gating: `strategies/coup.py :: _find_trigger`. Config: `config/strategies.yaml`.

---

## Step 0 — Pre-gate (both strategies)

Run on every closed 15m candle:

1. **High volume** — bar total volume ≥ `vol_mult × rolling median`.
   - coup: median over last **20** bars · coup_reversal: last **10** bars · `vol_mult = 1.8`.
2. **One-sided** (coup only) — `|delta| / total volume ≥ 0.35`.
   coup_reversal **skips** this (reversal bars are two-sided / low net delta).

Fail → not a candidate. Pass → run the extreme-zone test below.
Extreme zone = top or bottom **10%** of the candle's high-low range.

---

## Pattern A — coup (momentum): high-volume CONTINUATION candle

Candle direction selects the extreme:

| candle | look at | trigger condition | winner | who trapped |
|---|---|---|---|---|
| **bear** (c<o) | top 10% | aggressive SELL (bid) ≥ **1.5×** aggressive BUY (ask) | **SHORT** | buyers@top |
| **bull** (c>o) | bottom 10% | aggressive BUY (ask) ≥ **1.5×** aggressive SELL (bid) | **LONG** | sellers@low |

The *winning* side already dominates the extreme — this is a one-sided
continuation bar, NOT textbook absorption. (A/B confirmed: this is coup's real edge;
the textbook `canonical` variant backtests negative.)

**Eye test:** big body, well-above-average volume, closes near the extreme in the
winner's direction, winner-side aggression piled at that extreme.

---

## Pattern B — coup_reversal (reversal): wick-rejection candle

The user-observed pattern. All three must hold at one extreme:

1. **Wick rejection** — wick at the extreme ≥ **33%** of the bar range.
2. **Volume fight** — ≥ **10%** of the bar's volume traded in the extreme zone.
3. **Winner controls the extreme** — either:
   - aggressive buying wins there (`ask ≥ bid` at a rejected low → **LONG**;
     mirror at a high → **SHORT**), OR
   - the pusher was absorbed (`bid ≥ 1.5× ask` at a low that rejected up → **LONG**;
     `ask ≥ 1.5× bid` at a high that rejected down → **SHORT**).

**Eye test:** high-volume candle with a long wick rejecting a high/low, a heavy
two-sided fight at the wick tip, closing back the other way. Almost always flagged
`wick-trap`. Rare (~5 per 1500 BTC 15m bars).

---

## Step 2 — Confirmation (makes it tradeable, not just a candle)

The **next candle** must follow through in the winner's direction:
- winner-direction delta dominant: `|next delta| / next volume ≥ 0.25`, AND
- next close progresses **past the trigger's close**.

This is the `CONFIRMS` flag. It is **rare (4–17% of candidates) but ~100%
continuation when it fires** — the strongest single filter we have.

`coup` itself uses a slightly richer confirm (`Coup._confirm`): winner aggresses
*with result* — a fresh winner-side stacked imbalance OR price progress — within
`confirm_within` bars (coup 3, coup_reversal 2), with a CVD fallback.

---

## Manual recipe (any footprint platform)

1. Flag bars with volume ≥ ~2× recent average.
2. Isolate the extreme 10% of the bar by price.
3. Sum ask (buy) vs bid (sell) volume inside that zone.
4. coup → winner side dominant at extreme + big body.
   coup_reversal → long wick + volume fight + rejection close.
5. Take it only if the **next** bar pushes the winner way with delta past the close.

---

## On the dashboard

Layers panel toggles:
- **Coup Absorptions** — momentum candles. Arrow below=long / above=short, `cABS`.
- **Coup-Reversal Absorptions** — reversal candles. cyan=long / purple=short, `crABS`.
- **Confirmed Absorptions ★** — gold squares on ONLY the next-candle-confirmed subset
  (the high-continuation set). Both modes.

Hover any marker → full read: who trapped · extreme aggressor + buy% · ask/bid split ·
next-candle confirm + Δ · forward 6-bar MFE/MAE in ATR + CONT/fail.

Server scan: `server/routes/dashboard.py :: _build_detections._scan` (dedupes the
full stored history so markers span the whole loaded chart, not just the last 100 bars).

---

## Annotated examples (from data/reports/absorption_observations_granular.md)

**Confirmed → continued (the pattern working):**

- `BTCUSDT 2026-05-31 19:45 IST` — coup SHORT, bear volx5.04, Δ-500.9.
  buyers@top trapped; extreme aggressor sell. Next candle CONFIRMS (Δ-34.61).
  → MFE 3.74 ATR / MAE 0.11 ATR **[CONT]**.

- `XAUTUSDT 2026-05-26 05:00 IST` — coup SHORT, bear volx5.29, Δ-63.79.
  buyers@top trapped; aggressor sell. Next CONFIRMS (Δ-71.69).
  → MFE 12.82 ATR / MAE 0.0 ATR **[CONT]**.

- `BTCUSDT 2026-05-23 14:00 IST` — coup_reversal SHORT, vol×2.12, wick-trap.
  buyers@top trapped, fight at high (ask 75 / bid 85). Next CONFIRMS (Δ-506).
  → MFE 1.48 / MAE 0.91 ATR **[CONT]**.

**Candidate but next candle did NOT confirm → drifted against:**

- `XAUTUSDT 2026-05-23 12:30 IST` — coup LONG, vol×14.4, buy_frac 0.99 (extreme
  one-sided). Next did NOT confirm (Δ-23). → MFE 0.14 / **MAE 10.43 ATR [fail]**.
  Lesson: huge vol_ratio alone is not enough — without next-candle follow-through
  it bled badly.
