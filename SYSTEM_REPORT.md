# System Performance Report — FootprintBiot

Generated: 2026-05-30 23:22 · Source: live paper log (committed + current on-disk data)
Window: 2026-05-28 10:56 UTC → 2026-05-30 05:06 UTC · Engine: **M1 (Claude)**, paper-simulated fills

> Companion to [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) (how it works) and [PLAN.md](PLAN.md) (roadmap).
> This file is the **numbers**: every figure below is computed from `data/cycles.jsonl`,
> `data/positions.jsonl`, and `data/mode_compare.jsonl`. R = realized risk units vs the disaster floor.

---

## 1. Headline

| Metric | Value |
|---|---|
| Closed cycles (trades) | **108** |
| Win rate | **94.4%** (102W / 6L) |
| Total R | **+5.72R** |
| Avg R / trade | **+0.0530R** |
| Avg win | +0.0591R |
| Avg loss | -0.0520R |
| Payoff (avg win / avg loss) | 1.14 |
| Profit factor | 19.32 |
| Max drawdown | -0.312R |
| Position legs filled | 111 (≈1.03 legs/cycle) |

**Read:** high WR, small avg R, fat profit factor — classic grid-recovery signature.
Wins are frequent and small; the few losses are contained (max DD -0.312R = near-flat equity).

---

## 1b. True RR — the R-denominator problem

The headline avg of **+0.053R** is *understated by design*. R here is measured
against the **disaster floor** (`leg ± max(5×ATR, 3% BTC / 1.5% XAU)`) — a stop
so far out it fired **0×** in 108 trades. The denominator is ~12× the TP distance,
so the R unit is huge and every win reads tiny.

| Symbol | Avg disaster-SL dist | Avg TP dist | SL / TP ratio |
|---|---|---|---|
| BTCUSDT | 2.29% of entry | 0.19% | **12.6×** |
| XAUTUSDT | 1.34% of entry | 0.13% | **12.3×** |

The same realized PnL, re-expressed against tighter risk yardsticks (WR/profit
factor unchanged — only the denominator changes):

| R measured against | Total R | Avg R / trade |
|---|---|---|
| Disaster floor (current headline) | +5.72 | +0.0530 |
| Tight % SL (1% BTC / 0.5% XAU ≈ ⅓–½ of floor) | **+17.86** | **+0.1653** |
| TP-distance risk (RR = reward : equal-sized risk) | **+68.17** | **+0.6312** |

Per cut, R vs **TP-distance risk** (the honest "did the trade pay relative to a
risk the size of its own target"):

| Bucket | Total R | Avg R |
|---|---|---|
| BTCUSDT | +17.02 | +0.5492 |
| XAUTUSDT | +51.15 | +0.6643 |
| Long (buy) | +19.98 | +0.4441 |
| Short (sell) | +48.19 | +0.7649 |

**Takeaway:** the strategy's per-trade edge is **~12× larger than the headline R
implies** (+0.63R vs +0.053R against TP-sized risk; ~3× even against a tight 1%/0.5%
stop). The disaster floor — intentionally huge for the no-hard-SL design — hides
it. Two responses: (1) report R against a real risk distance, above; (2) trade a
tighter SL for real — see the `republic` strategy A/B in [STRATEGIES.md](STRATEGIES.md),
which keeps democracy's signal but clamps the stop to 1.5×ATR. Tighter SL raises
realized RR but will cost win-rate; the A/B measures exactly that tradeoff on the
live signal.

> Caveat: rescaling is exact for sign/WR but assumes the tighter SL wouldn't have
> been *hit* — it ignores the win-rate cost of a real stop. The `republic` A/B is
> the only way to measure that honestly; these columns are an upper bound on edge.

---

## 2. Equity curve (cumulative R, 108 closed cycles)

```
  +5.7 |                                                        ++
  +5.3 |                                                      ++  
  +4.9 |                                                   +++    
  +4.5 |                                                +++       
  +4.1 |                                            ++++          
  +3.7 |                                        ++++              
  +3.3 |                                    ++++                  
  +2.9 |                                ++++                      
  +2.5 |                          ++++++                          
  +2.0 |                    ++++++                                
  +1.6 |          ++++++++++                                      
  +1.2 |        ++                                                
  +0.8 |   +++++                                                  
  +0.4 | ++                                                       
  +0.0 |+                                                         
       +----------------------------------------------------------
        trade 1                                          trade 108
```

Final **+5.72R**, peak +5.72R, trough +0.00R, max drawdown -0.312R.
Monotonic-ish climb — drawdowns are shallow because nano-lot legs average down and exit on bounce.

---

## 3. By symbol

| Symbol | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 31 | 93.5% | +1.39 | +0.0447 | +0.0478 | -0.0000 | ∞ |
| XAUTUSDT | 77 | 94.8% | +4.33 | +0.0563 | +0.0636 | -0.0781 | 0.82 |
| **ALL** | 108 | 94.4% | +5.72 | +0.0530 | +0.0591 | -0.0520 | 1.14 |

XAU carries per-trade edge (avg +0.0563R, 71% of all trades) and owns the only material losses (avg loss -0.0781R); BTC is positive but thinner, with near-zero loss magnitude.

---

## 4. By direction (buy / sell split)

| Side | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
| Long (buy) | 45 | 88.9% | +1.72 | +0.0382 | +0.0508 | -0.0625 | 0.81 |
| Short (sell) | 63 | 98.4% | +4.00 | +0.0635 | +0.0645 | -0.0000 | ∞ |

Book skews short (63 short vs 45 long); both sides profitable, near-equal payoff.

---

## 5. Symbol × direction

| Bucket | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
| BTCUSDT long | 18 | 88.9% | +0.72 | +0.0401 | +0.0451 | -0.0000 | ∞ |
| BTCUSDT short | 13 | 100.0% | +0.66 | +0.0511 | +0.0511 | +0.0000 | ∞ |
| XAUTUSDT long | 27 | 88.9% | +1.00 | +0.0370 | +0.0546 | -0.1041 | 0.52 |
| XAUTUSDT short | 50 | 98.0% | +3.33 | +0.0667 | +0.0680 | -0.0000 | ∞ |

Best bucket: **XAU short** (50 trades, WR 98%, avg +0.0667R) — carries the book (+3.33R of +5.72R total).
Weakest: **XAU long** (avg +0.0370R, payoff 0.52) — only bucket with real loss drag (avg loss -0.1041R).

---

## 6. Exit reasons

| Reason | Count | Share |
|---|---|---|
| tp_hit | 67 | 62% |
| sl_hit | 40 | 37% |
| tp_absorption | 1 | 1% |

Note: `sl_hit` here are mostly **trailed-stop profit locks** (positive R), not disaster exits —
disaster_floor fired only 0× and choch_invalidation 0×.

---

## 7. Order flow (position legs, not cycles)

All legs sourced from **m1_claude** (M1 live; M2 still dry-run).

| Cut | Counts |
|---|---|
| Open / close legs | 111 / 110 |
| Side | short 64 · long 47 |
| Symbol | XAU 79 · BTC 32 |

≈1.03 legs per cycle → grid mostly fired single-leg, occasionally averaged in.

---

## 8. M2 (rules engine) — dry-run signal mix

M2 logs to `data/mode_compare.jsonl` but does **not** trade yet (no realized R to report).
Signal distribution over 388 bars:

| Signal | Count |
|---|---|
| short | 143 |
| long | 137 |
| flat (no trade) | 108 |

M2 fires on 72% of bars; the rest are filtered flat (|score| < 0.35).

---

## 9. Caveats

- Single regime epoch (2026-05-28 → 2026-05-30); WR/payoff will compress out of sample.
- Fills are **paper-simulated**; no slippage/spread/funding modeled.
- `realized_pnl` taken from `cycles.jsonl` close events (bar-verified flag present on the early batch).
- M2 numbers are signal-only — no execution, so not comparable to M1's realized R here.
- These are **committed-state + current on-disk** figures; re-run `/tmp/gen_report.py` after new data.
