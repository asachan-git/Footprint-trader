# FootprintBiot — Learnings, 2026-07-30

Forensic audit of Jun22 → Jul16 2026 live trading, built from: 6 real Vantage broker PDFs (22/23/24/25/26/29 June, account 26443422), the "Grid System — June Forensic Timeline" artifact, the "Trigger Lifecycle Reference" doc (config snapshot @ 2026-07-12), and the "Production Readiness — Exit Mechanics, TP Logic & P&L, Day by Day" artifact (deal-level parse of both broker accounts, Jun22–Jul16). Git commit hashes cited throughout are on the relevant live branch at the time.

**Known gaps as of this writing:** the "Production Readiness" artifact's tail (Jul16 onward) hit a hard WebFetch truncation limit — Jul17–20 day-by-day narrative and its "strategy/TF" table/recommendations section were not retrievable and are not included below. `80kTradeAccountTrades.pdf` (1582 pages) not separately re-parsed.

**Base-lot column corrected 2026-07-30**, replacing the artifact's approximations with real values parsed directly from both native MT5 HTML exports (`ReportHistory-22ndJuneTo29thJune2026.html`, UTF-16LE, Orders table; `ReportHistory-32255364.html`, same format). Method: regex over every Orders row for `(date, order type, first volume number)`, grouped by date, base_lot = the minimum/modal leg-1 rung of that day's ladder (ladder always scales up from base_lot, so the floor of each day's size distribution is the base). The artifact's "~0.01"/"~1.0" placeholders for this window were wrong for Jun22–29 specifically (real data: 0.25, not 0.01) — every other day's figure below is the real parsed value, not an estimate.

---

## 1. Which branch was actually live, when (account + branch cutover map)

| Window | Branch | Account | Notes |
|---|---|---|---|
| Jun22–29 | `feat/composed-strategy` | 26443422 (USC) | Abandoned after Jun26 (last real commit); Jun29 ran unmodified Jun26 EOD config |
| Jun29–Jul06 | `fix/trail-oneshot` / `feat/live-v2` (identical commit histories) | demo only | The demo-testing branch — BTC-regime alignment, zone-width sizing, deferred SL, candle_sweep/engulf. `fix/trail-oneshot` freezes at Jul06; `feat/live-v2` continues |
| Jul06–15 | `feat/live-v2` | 32255364 (new account, post Vantage HFT-abuse block on 26443422) | Includes the Jul14 disaster |
| Jul16+ | `feat/jun22-clean` | 32255364 (same account — branch cutover, NOT a second account switch) | Own commit message: "against the real Vantage account (32255364) for the first time" — refers to the branch, not the account |

`222cbd6` (Jul06, on `feat/live-v2`) is literally titled "account-switch cleanup," adds `ExecBridge.check_account_switch()`. Balance carry-forward confirmed: +79,547 / −79,547 pair in the deal log at Jul06 13:32:10, matching Jun29's close on the old account.

---

## 2. Per-day table — strategy/TF, config, results

Grid type is constant across every day below unless noted: **neutral straddle, `net_profit_exit_only: true`** (no leg TP alone closes a cycle — only the whole basket going net-positive does), `leg_tp_ceiling: true` (a computed structural TP still sits on each order as a far backstop) except where flagged.

| Date | Branch | Base lot (real, from HTML) | P&L | Win rate | Config changes that day | Character |
|---|---|---|---|---|---|---|
| **Jun22** | composed-strategy | **0.25** | **+8,174.18** | **78.0%** (71W/20L) | Baseline (`9590331`). `bias_trail`: $5 activate/40% giveback/book-half. `hvn_reversion_bias:true`. **No touch-arm** — candle-close confirmation only. | Thrived — best win-rate in the dataset |
| **Jun23** | composed-strategy | **0.25** | +6,250.25 | 59.6% (65W/44L) | 18:13 `cd08c79`: touch-arm born (5m/15m, `touch_arm_confirm_ticks:0.2`), `require_squeeze_gate false→true`, `cycle_net_target_usd 50→500`, `min_tp_dist:8.0` added | Survived — fix landed 90min after that day's giveback started |
| **Jun24** | composed-strategy | **0.25** | **+19,301.18** | 65.6% (84W/44L) | `8fba248`: TP was landing on the touched node's OWN boundary (un-profitable by construction) → fixed to next-HVN far edge; one-cycle-per-TF enforced. `0d20c54`: dedup-release bug fixed (closed cycle blocked its own re-arm forever) | Thrived — best June $ day |
| **Jun25** | composed-strategy | **0.25** | +8,948.50 | **51.1%** (95W/91L) | Full restructure to per-TF sizing (`cycle_net_target_by_tf`, `bias_trail_activate_by_tf` introduced). `modify_cooldown_s:100` (first order-rate throttle). `e3cb70f`: extreme-HVN TP → node center, not far edge | Bled — coin-flip win-rate, positive only because early trades carried the day |
| **Jun26** | composed-strategy | **0.25** | +8,643.25 | 62.5% (120W/72L) | No config change. `3ad9740`: bias-trail gate required ALL legs on a side open simultaneously — one TP fill permanently killed the trail for survivors. Fixed to keep trailing off the recorded peak | Thrived — last commit on this branch, abandoned same day |
| **Jun29** | composed-strategy (dead) | **0.35** | **−10,584.70** | 54.0% (116W/99L) | **Zero commits on the live branch.** Lot size itself jumped 0.25→0.35 with no corresponding commit found — config drift, not a tracked change. The real weekend-VP fix (`255bcbd`) landed same day but on `feat/grid-clean-baseline` — a branch that was never live | First loss — nothing wrong was fixed because nobody was watching this branch, and size crept up unwatched too |
| Jun30–Jul05 | fix/trail-oneshot / live-v2 (demo) | — | no live $ | — | `a42ce9e` (Jul03): **`defer_sl_on_half_fill` added, default false** — "net_target/full_hedge own the exit (grid-recovery thesis)." The committed side of a filled ladder is never hard-stopped, by design, from here through Jul14 | Philosophy locks in |
| **Jul06** | live-v2 | **0.75** | +7,979 | 75.2% (79W/26L) | `222cbd6`: account-switch detection; lot-weighted bias-trail gate; `fullfill_be_enabled:true`; `htf_bias`/`bb_edge` OFF ("SIMPLE" symmetric mode) | Thrived — clean migration. Base lot already 3x Jun22's, first day on new account |
| **Jul07** | live-v2 | **0.75** | **+56,066** | 68.0% (433W/204L) | `touch_arm_confirm_ticks 0.2→0.02`. `lvn_edge_touch` added. Strategies stop blocking each other (own forced-arm path per strat×TF, ends `candle_sweep` arming 0×). MTF-aggregation-freeze fix (sentinel bar had frozen 5m/15m ~2h) | **Best day of the whole window** — 6 fixes landed before the run |
| **Jul08** | live-v2 | **0.75** | −5,148 | 65.3% (275W/146L) | `modify_cooldown_s 100→60`. Leg caps widened. Real bug: `lvn_edge_touch` missing from chop-exemption (13 genuine taps skipped) | Bled — DD 44,707, worst DD/loss ratio yet |
| **Jul09** | live-v2 | **1.0** | +38,931 | 69.6% (323W/141L) | `htf_bias`/`bb_edge` restored true ("Jun22 restore"). `squeeze_enabled` turned back ON **against Jul07's own data** showing it net-negative on 1m/5m | Thrived. Base lot up again, 0.75→1.0 |
| **Jul10** | live-v2 | **1.0** | −3,885 | 69.0% (294W/132L) | `touch_arm_confirm_ticks 0.02→0` (raw tap, final cut). `089d45e`: venue-rebase formula algebraically collapsed to `live==bar_close` — HVN/LVN never armed on taps visibly happening on chart. Net-negative flatten guard **added 22:10, reverted 22:25 same night** | Unstable — 3 separate "chart shows it, system doesn't act" bugs fixed same day; DD 56,257 |
| Jul11–12 | live-v2 | — (zero orders — weekend, XAU doesn't trade) | — | — | Trigger Lifecycle Reference doc dated Jul12 — config snapshot for this era | No trading |
| **Jul13** | live-v2 | **1.0** | +20,887.33 | 68.5% (544W/250L) | `84315bb`: `lvn_edge_touch` touch buffer fixed (flat 1¢ → 0.02% price-relative — was unreachable on XAU). `23ec9f2`: VP warm-up 30-bar→5-bar floor | Thrived — highest trade count (795 deals). Per-strategy: `hvn` +63,796, `lvn_edge` −31,114.50, `hvn_edge` −8,599.50 |
| **Jul14** | live-v2 | **1.0→1.5** (confirmed exact in real data — clean 1.5/3.0/4.5/6.0/7.5/9.0/10.5/12.0/13.5 ladder) | **−126,416** | 63.4% (268W/155L) | **08:48 `5c71393` is the real config delta:** `base_lot 1.0→1.5`, `modify_cooldown_s 60→0` (throttle fully removed), `hvn_max_legs_by_tf {5,7,9,7}→{4,5,6,7}`, `hvn_touch_buffer_pct 0.1→0.02`, triggers retuned (`hvn_edge`/`lvn_edge`/`hvn_disp` OFF, `candle_sweep` ON), venue-rebase multiplicative→additive fix. **09:54 `8d9f546` changed NOTHING — comment-only, see §16** | **THE DISASTER.** Real loss was the **SELL side**, per user's direct account 2026-07-30 — user saw a big loss on sell and closed those positions manually. (Earlier automated-artifact parse claimed buy −$112,797/sell +$698 and I initially inferred an automated `leg_closed_other` flatten from the deal-timestamp clustering — both superseded; see §5 for the full revision history and what's still unresolved) |
| **Jul15** | live-v2 | **0.5** (NOT 0.01 yet — real data shows a 0.5-increment ladder: 0.5/1.0/1.5/2.0/2.5/3.0; the artifact's "1.5→0.01" claim is wrong for this specific day) | −33,983 | 60.0% (228W/152L) | `leg_tp_ceiling` flipped **false for the first time ever** ("stops the −203k catastrophic wrong-side leg_tp flatten"). `runner_ratchet_enabled` + `bias_book_loser_frac` introduced. Every trigger except `hvn_inside_touch` disabled | Damage control — heaviest single-day commit count (8), still lost money |
| **Jul16** | jun22-clean (cutover) | **mixed** — real data shows a fine 0.01–0.14 micro-ladder AND a separate coarser 0.25/0.5/0.75...3.0 ladder simultaneously (transition day, two sizing regimes visibly overlapping — likely old positions closing at old size while new arms use the new 0.01 base) | −845 | 69.0% (118W/53L) | Branch cutover to `feat/jun22-clean` | Held — detail beyond this point not retrieved (see gaps above) |
| **Jul17** | jun22-clean | **0.25** | — | — | Not yet retrieved from artifact | — |
| **Jul20** | jun22-clean | **0.25** | — | — | Not yet retrieved from artifact | — |

---

## 3. What consistently helped

- **`hvn_inside_touch`, neutral straddle, reversion skew** — the core engine every profitable stretch traces back to. Jun22 (its purest form, no touch-arm) is the best win-rate day in the dataset.
- **`bias_book_trail`** (introduced `d32794c`, Jun19) — fixes the "profit reversed to nothing because a hedge leg dragged the net-basket below target" failure. Its own Jun26 refinement (`3ad9740`, trail off the recorded peak even as legs close) mattered as much as the original mechanism.
- **Same-day reactive fixes, caught fast**: dedup-release (Jun24), TP-target-node-boundary (Jun24), MTF-freeze (Jul07), lvn_edge_touch buffer (Jul13), VP warm-up floor (Jul13). None glamorous — all "make the existing engine more accurate."
- **Per-TF sizing/targets** over one flat basket-wide number (Jun25 restructure) — more precise, though it also came with more moving parts.

## 4. What consistently harmed, or is a live open risk

- **`defer_sl_on_half_fill` / `fullfill_be_enabled`, off by design from `a42ce9e` (Jul03) onward** — "net_target/full_hedge own the exit" is the stated philosophy. This is the single thread connecting Jun29's loss, Jul08's DD blowout, Jul10's DD blowout, and Jul14's disaster: **the committed side of a filled ladder is never hard-stopped**, on purpose, for the system's entire live history through Jul15.
- **`leg_closed_other` — confirmed unguarded** ("No net-negative guard (removed 2026-07-10) — fires regardless of P&L," per the Trigger Lifecycle doc). This is a real, live, independent risk regardless of what actually happened Jul14 (see §5) — worth fixing on its own merits.
- **Order-rate throttle loosened right before the disaster** — `modify_cooldown_s`: 100 (Jun25) → 60 (Jul08) → 0 (Jul14, fully removed same week as the loss).
- **Sizing up right before a bad day** — `base_lot 1.0→1.5` on Jul14 itself.
- **Going against the system's own measured data** — `squeeze_enabled` turned back on Jul09 "against Jul07's own data showing squeeze arms net-negative on 1m/5m." Didn't hurt that specific day, flagged as a standing risk.
- **The single-side directional grid experiment** (`70b18a6`, Jun29 17:33 → reverted `2d22ee7`, Jul01) — converted `hvn_inside_touch` to sell-biased single-side, landed the same afternoon as that day's heaviest loss cluster (18:00–20:52).
- **Branches doing real work while not being the live one** — Jun29's actual fix (`255bcbd`) landed same-day but on a dead branch; the account that needed it kept running unmodified for a week.

## 5. Jul14 mechanism — settled by user's direct account, 2026-07-30

Revision history kept here so a future pass doesn't re-trust an intermediate version:

1. **Original artifact parse** (automated, from a broker PDF/HTML deal-level join): claimed BUY was the loser (−$112,797.18) and SELL finished flat (+$698.49).
2. **My own intermediate theory** (superseded): inferred an automated `leg_closed_other` `CLOSE_ALL` from deal-log timing — six legs closed within a 3-second window, same $4084–4087 price band, −$110,455.50 total. Reasoned that same-price/same-instant meant a server-issued flatten. **Wrong to conclude from timing alone** — a manual multi-leg close produces an identical signature.
3. **Final, per user's direct account (2026-07-30)**: **the loss was on the SELL side**, not buy, and **the close was manual** — user saw a big loss on sell and closed those positions themselves. This directly contradicts the artifact's automated buy/sell split (#1) — that parse's side-attribution should be treated as wrong, not the user's firsthand account.

**Net for future reference**: Jul14's disaster mechanism was a manual close of a losing SELL position, not an automated flatten and not (per this account) a buy-side loss. The automated deal-parse tools used earlier in this investigation (artifact + my own timestamp inference) got the side wrong — a caution about trusting automated broker-log parses over a direct account when they conflict.

**What IS still solid regardless of mechanism**: `sl=0.0` on every leg, every branch, is a structural gap independent of which side lost or how the close happened — no position, on any day in this whole window, ever had broker-side stop protection. `leg_closed_other`'s missing P&L guard (confirmed unguarded in code) is also a real, separate, still-open risk on its own merits, whether or not it fired Jul14 specifically. Both fixes stand regardless of how this mechanism question resolves.

## 6. Remaining gaps

1. Jul17–20 day-by-day narrative not yet retrieved (WebFetch truncation) — may need the content pasted directly or another retrieval path.
2. `80kTradeAccountTrades.pdf` (1582p) not separately re-parsed this pass.
3. Given the artifact's automated deal-parse got Jul14's buy/sell attribution wrong (§5), its other per-day $/win-rate figures in §2 should be treated as reasonably reliable but not beyond question — the lot-size column is now independently verified from raw HTML; the P&L/win-rate columns are not yet independently re-verified the same way.

---
---

# PART II — Config mechanics: what each knob does and how it moved

Added 2026-07-30. Sources, both hard: **(a)** `git show <sha>:config/settings.yaml` parsed at all 30 config commits Jun22→Jul16 across `feat/composed-strategy` → `feat/live-v2` → `feat/jun22-clean`; **(b)** the broker **Positions** table of live account 32255364 (`ReportHistory-32255364.html`, 3,959 positions with true direction + comment tag + P&L). Artifact material is used only where it doesn't conflict — the "Production Readiness" artifact's Jul14 buy/sell split was rejected outright (it inverts the side; see Part I §5).

## 7. Per-strategy × per-TF P&L — the decisive table

Account 32255364 lifetime (Jul06–20), parsed from Positions (not Deals — Deals inverts direction):

| Strategy | 1m | 5m | 15m | Lifetime | Win% |
|---|---|---|---|---|---|
| `hvn_inside_touch` | −27,462 (926) | +9,311 (1170) | **+29,010 (600)** | **+13,391** | 66.2% |
| `lvn_edge_touch` | −9,170 (130) | −6,846 (261) | **+23,956 (165)** | **+7,940** | 61.7% |
| `hvn_edge` | — | **−41,742 (165)** | −4,096 (65) | **−45,838** | 60.0% |
| `candle_sweep` | — | **−46,377 (252)** | −6,724 (122) | **−53,102** | 62.0% |
| `hvn_displacement` | — | −479 (8) | −3,853 (3) | −4,332 | 63.6% |
| `squeeze` | −150 (15) | −1,616 (9) | +874 (4) | −1,735 | 80.6% |

**By TF, all strategies: 15m +39,396 · 1m −36,522 · 5m −88,859.** 15m is the only profitable timeframe.

**Win rate is 60–66% on every strategy and every TF.** The differentiator is loss magnitude, not hit rate. Mechanism: node width scales with TF. A 15m HVN is wide enough that the ladder spans real price and mean reversion has room before a side fully commits; on 1m/5m the node is narrow, price crosses the whole ladder and keeps going. **Without a hard stop, low TFs are structurally the wrong place to run this.**

## 8. Setup lifecycle — when each was added/removed, and what it earned

| Setup | Added | Removed / toggled | Lifetime |
|---|---|---|---|
| `hvn_inside_touch` | Jun22 (all TF) | restricted to {1m,5m,15m} 07-03; never disabled | +13,391 |
| `squeeze` | Jun22 (all TF) | dropped 07-01; restored 07-16 by jun22-clean | −1,735 |
| `hvn_edge` | 07-03 {5m,15m} | **OFF 07-07 11:28 → ON 07-10 08:24** → OFF 07-14 → ON 07-15 00:59 → OFF 07-15 09:31 | −45,838 |
| `candle_sweep` | 07-03 (disabled) | **ON 07-07 11:28**, stayed on through 07-14 | −53,102 |
| `lvn_edge_touch` | 07-07 {5m,15m} | **extended to 1m 07-10**; OFF 07-14; ON 07-15 00:59; OFF 07-15 09:31 | +7,940 |
| `hvn_displacement` | 07-10 08:31 {5m,15m} | OFF 07-14 08:48 | −4,332 (11 positions) |
| `bb_expansion_touch` | 07-15 00:59 (inside the server-TP commit) | OFF 07-15 09:31, 8½h later | 0 closed positions |

**Two correlations worth naming:**

- The three days `hvn_edge` was **OFF** (07-07/08/09) are the account's best stretch: **+89,937**. It was re-enabled 07-10 and lost on every remaining day.
- `lvn_edge_touch` earned **+46,792 in its first three days** on {5m,15m}. On 07-10 it was extended to **1m** *and* its touch buffer was simultaneously made unreachable (see §10) — every day after was negative.

## 9. Entry criteria — candle-close vs intrabar touch, and the confirmation buffer

This is the variable with the cleanest link to win rate.

| Period | Arm mode | `touch_arm_confirm_ticks` | Best win rate in period |
|---|---|---|---|
| Jun22 | **candle-close only** — no touch-arm existed | n/a | **78.0%** (06-22) |
| Jun23 → 07-07 | touch-arm on {5m,15m}, later {1m,5m,15m} | **0.2** (price must revert $0.20 back inside the edge) | 75.2% (07-06) |
| 07-07 11:28 → 07-10 | touch-arm | **0.02** (10× tighter) | 69.6% (07-09) |
| 07-10 22:10 → onward | touch-arm | **0** — arm on the raw tap, no reversal wait at all | 69.0% max |

Mechanically `touch_arm_confirm_ticks` is a **rejection-confirmation filter**: after price taps a zone edge, it must retrace N dollars back inside before the grid arms. At 0.2 you only arm on taps that actually got rejected. At 0 you arm on every touch, including ones that blow straight through — which is exactly the setup that produces a fully-committed losing side.

**Your hypothesis is supported at the coarse level**: the candle-close-only regime (Jun22, no intrabar arming at all) has the highest win rate in the dataset by 3+ points. Between 0.2 → 0.02 → 0 the win rate drifts down modestly rather than collapsing, so buffer *size* is a weaker effect than the presence of bar-close confirmation itself. Treat "bar-close only" and "retracement buffer size" as **two separate variables** — the first has strong support, the second only weak support.

Also note which triggers were bar-close vs intrabar by design: `hvn_edge`, `candle_sweep`, `hvn_displacement`, `bb_expansion_touch` are **all bar-close** (no `touch_only`) — and all four are net negative. `hvn_inside_touch` and `lvn_edge_touch` carried `touch_only: true` for most of the live window and are the two positive ones. So bar-close-vs-touch does **not** cleanly separate winners from losers at the trigger level; TF does.

## 10. The small thresholds — every buffer, clamp and floor, and when it moved

These are the invisible knobs. Several were set to values that silently disabled a whole trigger.

**Tap-detection buffers (control whether an arm fires at all):**

| Key | History | Note |
|---|---|---|
| `hvn_touch_buffer` | 0.5 (06-23) → 0.1 (07-01) → **0.02** (07-06) → 0.2 (07-07) | 0.02 = 2 cents on gold — effectively unreachable, same failure class as the LVN bug below |
| `hvn_touch_buffer_pct` | 0.1 (06-26) → **0.02** (07-14) | width-relative term cut 5× on the disaster day |
| `lvn_touch_buffer` | 0.02 (07-07) → **0.01** (07-10) | with `_pct` → 0.0 the same day, this made LVN taps **unreachable** |
| `lvn_touch_buffer_pct` | 0.05 (07-07) → **0.0** (07-10) | removed the width term entirely |
| `lvn_touch_buffer_price_pct` | **NEW 0.0002** (07-13) | the fix — 0.02% of price ≈ $0.82; buffer reachable again |
| `hvn_edge_tap_buffer` | 0.3 (07-03) → 0.05 (07-07) | |

**`lvn_edge_touch` therefore had an unreachable entry buffer from 07-10 to 07-13** — the exact window in which it was also extended to 1m. Its P&L collapse dates from here.

**Distance/spacing floors:**

| Key | Value | Effect |
|---|---|---|
| `min_tp_dist` | 8.0 (NEW 06-23, never changed) | a structural TP closer than $8 to the outer leg is skipped and the walk continues outward. If nothing qualifies the TP can end up **zero** — this is the path the 1m-ATR sentinel bug exploited |
| `min_leg_gap_usd` | NEW 1.0 (07-15, in `b3615f6`) | hard minimum $ spacing between legs — **never reverted** with the rest of that commit |
| `max_fulcrum_dist_pct` | 0.05 → 0.01 (06-24) → 0.02 (06-26) | rejects a fulcrum more than this fraction of price from spot |
| `hvn_shift_min_frac_step` / `tp_refresh_min_frac_step` | 0.25 (07-03) | order-modify noise floors — sub-step wiggle never reaches the broker |

**ATR-related clamps and gates:**

| Key | Introduced | What it does |
|---|---|---|
| `expansion_atr_ratio` | 1.5 (07-01) | session ATR ratio required to confirm expansion |
| `zone_shift_max_usd` | 6.0 (07-15 11:07) | outlier gate on the venue↔analysis basis shift; paired with an ATR plausibility clamp |
| `feed_max_age_s` | 180 (07-15 10:14) | suppress fresh arms on a dead analysis feed |
| ATR sentinel filter | code, `35b5148` (07-15) | forming bar `close_ts=9999999999` carried a stale price → 96pt phantom true-range → 1m ATR read 7.85 vs true ~0.8 → inflated step → outer leg overshot the TP → `min_tp_dist` guard zeroed it → **1m legs placed with tp=0** |

That last chain is the most instructive bug in the dataset: a data-hygiene defect (sentinel bar) propagated through ATR → leg spacing → TP guard → **no take-profit at all**. Four layers, each individually reasonable.

**VP / zone-shaping knobs** (change which zones exist, therefore every arm):
`vp_hvn_rel_height` 0.5, `vp_smooth_price` 0.7 (06-26) · `vp_merge_gap_bins` 20, `vp_merge_trough_ratio` 0.55 (07-01) · `hvn_stab_enabled`/`hvn_stab_edge_tol_frac` 0.25/`hvn_stab_del_n` 3 (07-03, hysteresis so zone edges stop jittering) · `hvn_fade_confirm_n` 3, `hvn_fade_strike_min_gap_s` 240 (07-03) · `vp_merge_max_width`, `vp_merge_overlap_frac` 0.5 (07-16) · `vp_min_bars` 30→5 (07-13, `23ec9f2` — cached VP needed 30 bars before building, so `/exec/zones` sat empty ~30min after every session roll and nothing could arm).

**Broker-rate throttle:** `modify_cooldown_s` 100 (06-26) → 60 (07-08) → **0** (07-14).

## 11. bias_trail — one-sided vs both-sided, and what changed

| Version | Commit | Scope | Behaviour |
|---|---|---|---|
| v1 | `d32794c` 06-19 | **per-side** | Tracks the peak of the committed side; on giveback, books `bias_book_frac` (0.5) of **that one side** via CLOSE_SIDE and MOVE_BEs the remainder. Fires **once per cycle**. |
| v2 | `8766961` **06-27 01:27** | **cycle-wide, both sides** | Tracks combined buy+sell peak against one activate threshold. **Both bias AND hedge sides get independent MODIFY_SL** on each new peak. Per-TF activate + separate giveback for each side. Stateless — fires from EA poll data directly. |
| — | `c34949b` 07-24 | whole-cycle collapse | trail fires `CLOSE_ALL` instead of partial-book |
| v3 | `9c85598` / `f28db41` 07-24 | **back to per-side partial-book** | reverted; now asserted by test so it can't silently drift |

**The live window ran v2 (cycle-wide, both sides) for its entire duration** — it landed Jun27 01:27, a weekend, so its **first live session was Jun29: the first losing day (−10,068, an all-day bleed)**. It then remained in force through the whole Jul06–20 era. v1 (per-side) was only ever live Jun19–26 — the unbroken green stretch.

That is a correlation, not proof — Jun29 also ran on an abandoned branch with no monitoring. But the direction is consistent: the per-side trail books a *winner* and leaves the rest riding; the cycle-wide version trails **both** sides off a *combined* peak, which means a hedge leg's movement can trigger action on the winning side and vice-versa. Under `net_profit_exit_only` (no per-leg exit) that couples the two sides' fates in a way v1 didn't.

Later additions in the same family: `runner_ratchet_enabled`/`_activate_usd` 300/`_giveback_pct` 40 (07-15) — trails a BE runner to a profit lock; `bias_book_both_sides: true` + `bias_book_loser_frac: 0.5` (07-16) — books the **losing** side too at trail fire.

**`bias_trail_activate_usd` / `giveback_pct` churn:** activate went 5 → 750 → 1000 → 500 → 1000 → 600 → 250 → 10 (eight values). Giveback went 40 → 50 → 35 → 40 → 50 → 35 → 50 → 30 (**seven values**). Neither was ever held constant long enough to measure.

## 12. Per-TF parameters — the three maps that were fought over

**Leg count `hvn_max_legs_by_tf`:**

| Date | Map | Flat `hvn_max_legs` |
|---|---|---|
| 06-22 | — | 8 |
| 06-23 | — | 6 |
| 06-24 | {1m:4, 5m:5, 15m:6, 1h:7} | 6 |
| 07-03 21:00 | `{}` cleared | 8 |
| 07-06 | {4,5,6,7} restored | 8 |
| 07-07 12:50 | `{}` cleared | 6 |
| 07-08 14:32 | **{1m:5, 5m:7, 15m:9, 1h:7}** — widened | 6 |
| 07-10 22:10 | {5,7,9,7} | 8 |
| 07-14 08:48 | **{4,5,6,7}** — narrowed | 6 |
| 07-16 (jun22-clean) | — | 8 |

The widened `{5,7,9,7}` map was in force exactly across the elevated-drawdown stretch (07-08 DD 44,707 → 07-10 DD 56,257). Narrowed back on 07-14 — but `base_lot` went 1.0→1.5 the same morning, so exposure per cycle *rose*.

**Step spacing `mean_rev_step_mult_by_tf` — the inverted map:**

Introduced 07-03 as `{1m: 1.0, 5m: 0.4, 15m: 0.2, 1h: 0.1}`. **This is backwards**: higher timeframes got *tighter* spacing. A 15m node is wide, so 0.2×ATR bunches the entire ladder into a small fraction of it. Cleared 07-03 21:00, **reinstated 07-06**, finally killed 07-07 12:50 in favour of flat 0.50 — **and 07-07 is the +56,066 day.** Flat 0.50 held from then on (brief 0.4 on 07-15).

**Targets `cycle_net_target_usd`** went: 50 → 500 → 5000 → 100 → 5000 → 7500 → 10500 → 10000 → **40000** → 20000 → 5000 → 100. **Twelve values in four weeks.** The 40,000 on 07-14 was the dead per-TF fallback (all four TFs had explicit 3,000–10,000 targets), so it never bound — but it shows how far the knob drifted from anything measured.

## 13. The pattern — why none of it could be evaluated

Counting config keys changed per commit:

| Commit | Date | Keys changed | Keys added |
|---|---|---|---|
| `2d22ee7` | 07-01 18:20 | **15** | 0 |
| `a42ce9e` | 07-03 19:43 | **12** | **12** |
| `222cbd6` | 07-06 18:36 | 10 | 0 |
| `089d45e` | 07-10 08:24 | **13** | 5 |
| `5c71393` | 07-14 08:48 | **10** | 1 |
| `b3615f6` | 07-15 00:59 | 10 | 4 |
| `35b5148` | 07-15 09:31 | **11** | 2 |
| `4800a61` | 07-16 07:38 | 8 | 4 |

**Changes arrived in bundles of 10–25 keys.** Not one commit in the entire live window changed a single variable in isolation. Every result is therefore a joint outcome of a dozen simultaneous edits, and no individual knob's contribution is recoverable from this data — including the ones this document identifies as most likely causal.

Compounding it, keys **oscillated**: `hvn_reversion_bias` T→F→T, `squeeze_enabled` T→F→T, `htf_bias_enabled` T→F→T, `bb_edge_enabled` T→F→T, `hvn_max_legs` 8→6→8→6→8→6, `mean_rev_step_mult` 0.3→0.5→0.3→0.5→0.4→0.5. A knob that oscillates cannot accumulate evidence.

**The structural reason this matters:** total exposure = `strategies × TFs × n × lot`, four multiplicative terms, each tuned separately on different days for different reasons. On 07-10 that was 5 strategies × 3 TFs × up to 9 legs × 1.0 lot — with no hard stop anywhere in the system. The individual edits each looked locally reasonable; the product did not.

## 14. What was changed on `feat/crude-hvn-rotation` today (2026-07-30)

Acting on §7: added `lvn_edge_touch` restricted to `15m` alongside the existing `hvn_inside_touch` `15m`. Both bar-close (no `touch_only`). Verified `lvn_touch_buffer_price_pct: 0.0002` is present so the LVN buffer is reachable (the 07-10→07-13 unreachable-buffer bug is not re-introduced). `hvn_max_legs: 6`, `mean_rev_step_mult: 0.5` flat, `base_lot: 0.25` unchanged.

Expected shape from history: **+52,966** on the same market that produced the account's actual **−86,858**, with Jul14 at −9,909 instead of −126,967.

**Caveat, stated plainly:** that figure is retrospective subset selection on in-sample data, and the counterfactual isn't exact — removing 1m/5m cycles frees margin and removes cross-TF hedging, so live results will differ from summing historical 15m rows. The effect is large and mechanically explicable, which is why it's worth acting on, but it has not been validated out-of-sample.

## 15. Rules that follow from all of this

1. **Change one variable at a time.** The single largest deficiency in this dataset is that nothing is attributable.
2. **Never disable a protection to run an experiment.** Two of two catastrophic events (Jul14 SL-disable, Jul20 R-breaker disable) followed directly from this.
3. **Exits belong at the broker.** Broker TP survived the Jul14 freeze; the server-side replacement explicitly could not, and its own commit message said so.
4. **Don't bundle a new strategy into a fix commit.** `b3615f6` shipped `bb_expansion_touch` live inside an emergency TP redesign, and the partial revert left `min_leg_gap_usd` and the ATR leg-spacing change behind.
5. **Thresholds can silently disable a trigger.** `lvn_touch_buffer 0.01` and `hvn_touch_buffer 0.02` both made taps unreachable on gold. Any buffer expressed in absolute dollars needs a price-relative floor.
6. **Size is the last thing to raise, not the first.** Lot rose into both disasters (1.0→1.5 on Jul14 itself) and was cut hard after both.

## 16. CORRECTION — `8d9f546` was a comment-only commit (verified 2026-07-30)

**The claim "Jul14 was caused by disabling the SL that same morning" is false.** It appears in at least three independent places — the *Production Readiness* artifact, the *Grid_System_Daily_Report* PDF ("protective SL turned OFF same morning"), and earlier revisions of this very document. All three read the commit **subject line** instead of the diff.

`8d9f546` (2026-07-14 09:54:30 IST, one file, 13 lines) touched `config/settings.yaml` only, and the diff is **entirely comment text**. `fullfill_cancel_opposite: false` and `defer_sl_on_half_fill: false` appear as unchanged *context* lines; `fullfill_be_enabled: false` had only its trailing inline comment stripped. **Zero behavioural change.** Its message ("all -> false") describes the state it documented, not a transition it performed.

Verified by reading the values at every config commit:

| Flag | Actual history |
|---|---|
| `fullfill_be_enabled` | `true` 06-23 (`cd08c79`) → **`false` 07-01 18:20 (`2d22ee7`)** → never `true` again |
| `fullfill_cancel_opposite` | `true` 06-24 (`e0684ea`) → **`false` 07-01 18:20 (`2d22ee7`)** → never `true` again |
| `defer_sl_on_half_fill` | **introduced already `false` 07-03 19:43 (`a42ce9e`)** — `true` at no point in the project's history |

**The real disable was `2d22ee7`, July 1st 18:20 — thirteen days before the disaster.** And it cannot be causal for Jul14, because the identical flag state was in force during the account's two best days: Jul07 (+56,066) and Jul09 (+38,931).

**Implication for any restore plan:** "revert `8d9f546`" is a no-op. The live decision is whether to re-enable `fullfill_be_enabled` (off since Jul01) and `defer_sl_on_half_fill` (never on) on their own merits — noting that `fullfill_cancel_opposite` carries real gap risk (it cancels the hedge, and `MOVE_BE` does not fill at breakeven through a news gap), and `defer_sl_on_half_fill` only survives a freeze if it armed *before* the freeze started.

**Methodological note:** this is the third factual error found in derived summaries during this audit — after the inverted Jul14 buy/sell split and the wrong base_lot column. All three were caught by going back to the primary source (git diff, broker Positions table). **Derived artifacts, including ones generated recently and confidently, must not be trusted over raw sources on any load-bearing claim.**

## 17. Order-level exit attribution — the decisive finding (2026-07-30)

Every position in account 32255364 classified by how it actually exited, by grouping positions into cycles (same day+strat+TF, closes within 90s) and testing: closed at its own stated TP → `structural_TP`; cluster net ≥ that day's configured `cycle_net_target_by_tf` → `net_target`; single-sided multi-leg close → `side_book` (bias_trail CLOSE_SIDE); other multi-leg close → `flatten/hedge`.

| Exit type | Golden run Jul06–13 | Blowup Jul14–20 | Combined |
|---|---|---|---|
| `structural_TP` | +237,047 (440) | +49,049 (167) | **+286,096** |
| `net_target` | +234,127 (380) | +54,908 (131) | **+289,035** |
| `side_book` (trail) | +187,662 (928) | +49,940 (378) | **+237,602** |
| **`flatten/hedge`** | **−521,908 (1088)** | **−344,162 (353)** | **−866,070** |
| single/other | −21,690 (36) | −11,832 (58) | −33,522 |

**The three intended exits earned +812,733 across the account's entire life. Flatten events lost −866,070.** Every mechanism the system was designed around works. The losses come almost entirely from baskets being dumped before they could resolve — `leg_closed_other`, `full_hedge`, and manual end-of-day flattening.

### Per strategy × TF, golden run

| strat/TF | structural_TP | net_target | side_book | flatten/hedge | Total |
|---|---|---|---|---|---|
| hvn/5m | +64,330 (120) | +77,571 (90) | +51,024 (232) | −126,636 (292) | **+63,446** |
| hvn/1m | +54,633 (110) | +69,291 (92) | +39,901 (218) | −112,365 (270) | **+51,270** |
| hvn/15m | +45,288 (68) | +26,160 (49) | +56,068 (169) | −80,347 (161) | **+40,719** |
| lvn_edge/15m | +12,833 (16) | +23,152 (50) | +6,219 (34) | −9,670 (45) | **+29,896** |
| hvn_edge/15m | +7,692 (7) | — | +4,510 (17) | −15,454 (30) | −3,252 |
| hvn_edge/5m | +8,741 (15) | +4,334 (9) | +3,981 (32) | −20,767 (61) | −3,711 |
| candle_s/15m | +10,839 (21) | +10,735 (10) | +12,993 (51) | −40,584 (9) | −5,780 |
| lvn_edge/5m | +18,144 (38) | +10,999 (38) | +13,392 (55) | −49,921 (119) | −7,148 |
| candle_s/5m | +4,816 (22) | +6,935 (8) | **−3,012 (93)** | −42,576 (58) | **−36,153** |

**`hvn_edge` is exonerated.** Intended exits: +29,258 combined across both TFs. Flatten: −36,221. It was never a losing strategy — it was a strategy that kept getting flattened before it finished.

**`candle_sweep/5m` is not.** It is the only cell in the table with a **negative side_book** (−3,012) — it loses even on its trail exits, before any flatten. Genuinely broken.

### Cycle completion rate

Of 534 multi-leg cycle groups in the golden run: **61 (11.4%) reached net_target**, 297 exited positive-but-short, 176 net-negative. Among target-reachers, mean overshoot **1.71×**, median 1.23×, max 8.49× — **+107,164 booked beyond target**, confirming the slippage-overshoot mechanism at order level. Of 2,872 positions carrying a broker TP, only **440 (15.3%)** closed at it.

Everything that is not a target-reaching cycle nets **−226,775**. The 11.4% carry the system.

### What this means for stops

The edge and the tail are the same mechanism: cycles must ride drawdown to reach the directional resolution that pays 1.71×. **`fullfill_be_enabled` and `defer_sl_on_half_fill` would both suppress exactly that** — BE-scratching or node-edge-stopping the committed side before the break. Earlier sections of this document recommended enabling them; **that recommendation is withdrawn.** The "grid-recovery thesis" is supported by the order-level data.

Protection must therefore come from levers that don't shorten cycles:
1. **Size** — no downside to the mechanism.
2. **Strategy selection** — cut what loses on its own exits (`candle_sweep`).
3. **Session gate** — 07–12 and 17–24 UTC were net-negative windows.
4. **Reduce flatten events** — the −866,070 bucket. `leg_closed_other` has no P&L guard; EOD manual flattening is discretionary.
5. **Disaster-only broker SL** — set beyond normal excursion so it never fires in consolidation; exists solely for the freeze case.

## 18. Sizing the disaster stop — from adverse-excursion data (2026-07-30)

**Method note — one approach failed and was discarded.** Reconstructing max adverse excursion from Binance 1m bars was attempted first, with a per-day venue-offset correction and an empirically-solved broker→UTC shift (confirmed UTC+3, median mismatch $2.79). It only reconciled with realised losses on **56%** of adverse positions. It fails precisely where it matters: during violent spikes the two venues diverge transiently, so bar-derived MAE understates the excursion on exactly the days worth measuring. **Those numbers are not used.**

The measure used instead needs no venue alignment: **the ladder is its own ruler.** If legs 1…n on one side all filled, price provably travelled from leg 1's price to leg n's price against leg 1. Pure broker data.

### Ladder span, golden run (832 same-side records, ≥2 legs)

| | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| Cycles that **recovered** | 3.3 | 7.4 | 9.0 | 12.2 | **15.4** |
| Cycles that **lost** | 2.8 | 5.4 | 6.9 | 9.6 | 10.3 |

By TF (recovered): 1m max 12.7 · 5m max 11.5 · **15m max 15.4** (15m legitimately needs the most room — wider nodes).

Counter-intuitive but informative: **losing cycles have *smaller* spans than recovering ones.** They lose by being flattened early, not by price running far — which corroborates §17's finding that flatten events, not adverse moves, are the loss driver.

### Stop-distance tradeoff

| D | Recovering sides cut | Losing sides caught |
|---|---|---|
| $10 | 21 (3.9%) | 1 |
| $15 | 1 (0.2%) | 0 |
| **$20** | **0 (0.0%)** | 0 |
| $30–50 | 0 | 0 |

**Recommendation: $20/oz per-leg disaster stop.** Zero interference — no recovering cycle in the golden run ever travelled 20 against its first leg (observed max 15.4, p99 12.2). At `base_lot 0.25` that is **$500 risk per leg**, ~$3,000 per fully-filled 6-leg side. It is a disaster bound, not a strategy stop: in normal operation it should never fire.

### Honest limit — a stop does not save you from a gap

Jul14's move was **+63 points in one minute** (12:31 UTC bar: open 4025.8, high 4089.1). A $20 stop on a sell entered near 4024 would have *triggered* at ~4044 but *filled* in the gap at ~4082–4089 — capping the loss at roughly what actually happened. **It would not have materially saved that specific day.**

Its real value is different and still worth having:
1. It survives a server/bridge freeze — broker-side, fires without the server.
2. It bounds slower adverse drift, which is the more common failure.
3. It converts an unbounded tail into a known per-leg budget, which is what makes position sizing calculable at all.

Jul14 spans confirm the gap mechanism: the largest spans that day were **buy** sides (19.4, 19.4, 18.4) and all three were *profitable* (+13,438 / +33,525 / +14,002). The catastrophic sells had spans of only ~7 — their ladders never filled out. Price simply jumped past the few legs already open. **A gap is not an excursion, and no stop distance defends against it.** Only size does.

## 19. Parameter optimisation — step, n, node width, whipsaw, trail (2026-07-30)

**June is now included.** Earlier sections used only the July account for position-level work; that was an omission. `ReportHistory-22ndJuneTo29thJune2026.html` carries a full Positions table in the same format (comments without a TF field — the single-magic era). June: **935 positions, net +40,733**, same $6/lot commission.

| June strategy | Net | n | Win% |
|---|---|---|---|
| `hvn` | +28,308 | 619 | 58% |
| `hvn_disp` | **+7,714** | 37 | 59% |
| `hvn_edge` | **+2,297** | 210 | 56% |
| `sqz` | −6,670 | 60 | 52% |

`hvn_edge` and `hvn_displacement` were **profitable in June and negative in July** — era-dependent, not intrinsically broken. Combined with §17 (hvn_edge +29,258 on its own exits vs −36,221 from flattens), the "exclude hvn_edge" call from §7 is withdrawn.

### Transaction-cost floor

Commission **$6/lot round trip = $0.06/oz**; configured `max_spread` 0.50 $/oz. **Round-trip cost ≈ $0.56/oz per leg.** Any step must clear this by a wide margin or the grid trades its own friction.

### Step, with the setups each bucket is drawn from

| Step $ | n | Net | Win% | Dominated by |
|---|---|---|---|---|
| 0–1.0 | 230 | −232,125 | 60% | hvn/1m×78, hvn/5m×63 |
| 1.0–1.5 | 202 | −170,739 | 67% | hvn/5m×66, hvn/1m×50 |
| 1.5–2.0 | 129 | +42,733 | 73% | hvn/5m×47, hvn/1m×27 |
| 2.0–3.0 | 150 | −55,729 | 78% | hvn/1m×48, hvn/5m×44 |
| 3.0+ | 89 | +53,532 | 80% | **hvn/15m×31** |

**Win% is monotonic 60→80% and holds independently in both eras** (June 52→73%, July 58→78%). **P&L is not monotonic** — the 2–3 bucket is negative because it is mostly 1m/5m cycles from the blowup period. An earlier draft claimed "step is the dominant parameter" from the July golden run alone; **that is overstated.** What survives: a hard cost-driven floor of step ≥ ~1.5, and the observation that the 3+ bucket is almost entirely 15m.

### ATR by TF, and why 1m cannot work

ATR(14) over Jul06–13: **1m 1.10 · 5m 2.50 · 15m 5.00.**

| TF | 0.50× | 0.75× | 1.00× | 1.25× |
|---|---|---|---|---|
| 1m | **0.55** | 0.82 | 1.10 | 1.37 |
| 5m | 1.25 | 1.88 | 2.50 | 3.12 |
| 15m | **2.50** | 3.75 | 5.00 | 6.25 |

At 0.50×ATR the 1m step is **$0.55 against a $0.56 round-trip cost** — the 1m grid was trading at exactly its friction. Even 1.25×ATR only reaches 1.37, still under the floor. **1m is structurally unviable on gold at this cost base, at any sane multiplier.** 15m clears the floor at the current 0.50×.

### Node width (ladder span) and whipsaw — counterintuitive

| setup/TF | cycles | both-sided | minority share | Net | Avg/cyc | med span | med step |
|---|---|---|---|---|---|---|---|
| candle_s/15m | 17 | 35% | **0.112** | −14,512 | −854 | 4.71 | 1.65 |
| hvn_edge/15m | 16 | 38% | 0.130 | +5,566 | +348 | 5.78 | 2.30 |
| candle_s/5m | 37 | 41% | 0.138 | −21,947 | −593 | 2.92 | 1.30 |
| hvn/single (Jun) | 26 | 54% | 0.161 | +24,651 | +948 | 3.90 | 1.25 |
| **hvn/15m** | 76 | 59% | 0.195 | **+90,455** | **+1,190** | 4.72 | 1.81 |
| hvn/5m | 125 | 68% | 0.218 | +56,374 | +451 | 3.73 | 1.39 |
| hvn/1m | 114 | 69% | 0.224 | −6,400 | −56 | 3.28 | 1.23 |
| **lvn_edge/15m** | 19 | 79% | 0.225 | +25,228 | **+1,328** | **8.79** | **2.25** |

**Fewest opposite-side fills does NOT mean best.** `candle_sweep` is the cleanest directionally (11–14% minority share) and loses. `lvn_edge/15m` is the most two-sided (79% both-sided) and has the best average per cycle.

Mechanistically consistent with §17: the profit engine is net_target, computed on the **basket**. A one-sided fill has no basket to resolve — it just rides to a TP or a flatten. **The hedge is not drag, it is the machine.** This is also the structural reason `candle_sweep` — a one-sided breakout grid by design — never fitted this system.

Widest ladders earn most per cycle (`lvn_edge/15m` span 8.79 → +1,328/cycle); narrowest lose most (`hvn/1m` span 3.28 → −56/cycle).

### ATR timing and the absence of a contracting ladder

**ATR is read once, at arm time** — `atr_from_store(symbol, tf)` inside `plan_grid_levels` (`grid_planner.py:582`) feeds `_size_grid`, which fixes `step`. It is **never recomputed for a live cycle.**

Post-arm, only two things modify resting legs, and neither changes spacing:
- `enqueue_modify_pending(price_delta=…)` — adds the *same* delta to every leg. A rigid parallel translation of the whole ladder to re-track a drifting HVN edge (`hvn_shift_min_frac_step` 0.25 noise floor). Spacing is invariant.
- TP refresh — touches TP only.

**A contracting/expanding ladder that re-spaces legs as ATR changes has never existed in this codebase.** Verified by git search over all branches: no commit implements ladder re-spacing, and no code path recomputes `step` after arm. The nearest relative is `reanchor_pending_when_frozen` (`e1ca0bd`, 2026-07-07 14:08), which re-anchors resting pendings each bar-close on a frozen cycle — again a translation, not a contraction.

**Implication:** a cycle armed in high volatility keeps its wide spacing even after volatility collapses (legs then sit too far out to fill), and a cycle armed in quiet conditions keeps tight spacing into an expansion (legs fill instantly, straight through). This is a real unexploited axis — but it is a code change, untested, and adding it now would violate the one-variable rule.

### Config applied to `feat/crude-hvn-rotation`

| Param | Before | After | Basis |
|---|---|---|---|
| `bias_trail_activate_usd` | 166.75 | **250.0** | `k·ATR·lot·contract·legs` = 0.5·5.0·0.25·100·4. Replaces a fixed-$ figure that had taken 8 values |
| `bias_trail_giveback_pct` | 40 | **40** (unchanged) | best per-book average in the data (+1,181 vs +875 at 50%) |
| `mean_rev_step_mult` | 0.50 | **0.50** (unchanged) | already yields 2.50 on 15m, above the 1.5 cost floor. June data does **not** support pushing to 0.75, and doing so would widen span past the observed 15.4 envelope and the $20 disaster stop |
| `hvn_max_legs` | 6 | **6** (unchanged) | the legs→profit relationship is likely endogenous (deep fills ⇒ a real move happened); not acted on |
| `hvn_edge` 15m | absent | **still absent** | rehabilitated by the data, but crude just added `lvn_edge_touch`. Adding a second trigger in the same change would make neither attributable. Queue it as the next single change |

**Deliberately not changed.** Three parameters the data hints at but does not establish, held back so the `lvn_edge_touch` addition stays measurable. That restraint is the §13 lesson applied.

### Queued next single change — TP-refresh divergence (noted 2026-07-30)

`33437a4` (Jul16 23:01, on `feat/jun22-clean`) gated `_refresh_cycle_tp` behind `tp_refresh_enabled`, **default false** — *"Orders are now STATIC once armed; the TP set at arm time no longer chases the HVN/LVN structure if it drifts."* That was the last live system's deliberate choice.

**Crude descends from a different lineage and never received it.** `tp_refresh_enabled` is absent from crude's config and the flag does not appear anywhere in its `server/routes/exec_bridge.py` — so **dynamic TP-refresh runs unconditionally on crude today.** Combined with `reanchor_pending_when_frozen: true`, crude currently re-tracks both pending leg prices *and* their TPs as structure drifts, while leg spacing stays frozen at arm-time ATR.

Not necessarily wrong, but it is an undecided divergence from the last known-good configuration. **Queued as the change to make after `lvn_edge_touch` has been measured** — not now, to keep that addition attributable.

## 20. Did `min_tp_dist: 8.0` help? — mostly no, because it doesn't do what its name implies

Introduced `cd08c79` (Jun23 18:13), value 8.0, **never changed since**. Jun22 is the only live day without it, giving a natural before/after.

| | n w/TP | <$4 | $4–8 | ≥$8 | median | TP-hit% |
|---|---|---|---|---|---|---|
| **No guard** (Jun22–23am) | 174 | 23% | 16% | 61% | **9.8** | 34% |
| **Guard 8.0** (Jun23pm→Jul20) | 4,569 | 23% | 23% | 54% | **8.8** | 16% |

**The guard did not reduce short TPs.** Sub-$8 distances went from 39% → 46%, and median TP distance *fell* 9.8 → 8.8. Reason: `min_tp_dist` is measured **from the outermost leg**, and `enqueue_grid_plan` gives every leg on a side a **shared TP price**. Inner legs are therefore mechanically closer to that TP; the guard never constrains them.

### Outcome by TP distance from each leg's own entry

| TP dist | n | Net | Avg | TP-hit% | Win% |
|---|---|---|---|---|---|
| 0–2 | 530 | −33,963 | −64 | 63% | 66% |
| 2–4 | 564 | −33,039 | −59 | 35% | 66% |
| 4–6 | 550 | **+26,253** | +48 | 19% | 67% |
| 6–8 | 509 | **+53,055** | +104 | 11% | 67% |
| 8–12 | 808 | −15,139 | −19 | 7% | 66% |
| 12–20 | 797 | **+61,036** | +77 | 3% | 63% |
| 20+ | 985 | −67,137 | −68 | 1% | 57% |

Positions whose TP sat within $4 of entry are net **−67,002** across 1,094 positions, despite hit rates of 35–63%. Per *actual* hit, payoff rises monotonically with distance (0–4: +251 · 4–8: +709 · 8–12: +931 · 12–20: +1,613 · 20+: +1,970) — but the extra frequency of short TPs does not compensate (528 hits × 251 = 132,652 vs 161 × 709 = 114,086, i.e. similar totals from 3× the hits).

**Causality caveat.** Because the TP price is shared per side, a *short* TP distance is mechanically the signature of a **deep leg in a committed move** (the last-filled leg sits nearest the shared target). So short-TP positions are largely a *symptom* of being deep in a ladder, not proof that a short TP *caused* the loss. The correlation is real; the direction is not established.

**Verdict:** the intent was right — the sub-$4 band is genuinely net-negative — but the implementation doesn't reach the legs that matter. A per-leg floor (each leg's TP ≥ X from *its own* entry) would actually express the intent. That also aligns with the design: under `net_profit_exit_only`, the leg TP is meant to be a far backstop, and an inner leg booking out early fragments the basket before `net_target` can resolve it (§17). **Not changed — logged as a candidate, not applied.**

---

## 21. Recreating CVD-divergence state on historical cycles — the alignment filter is real

**Question:** if we re-score every past cycle for whether a CVD divergence was present at arm time, do the aligned cycles show a better win rate?

**Method.** The detector is causal — `delta_divergence.detect(bar, history, window)` and `from_store` feeds it `bars[-1], bars[:-1]`, so nothing after the arm bar leaks in. Divergence state was therefore reconstructed at each cycle's arm timestamp from the 15m `XAUTUSDT` footprint (8,506 bars, complete back to 2026-05-07), and joined to broker cycles parsed from all four MT5 `ReportHistory` HTML exports (5,865 deduped positions → **616 cycles of ≥3 legs**, June 77 / July 539). Cycles were grouped on open time (15-min arm window), the grouping fixed in §19.

### Win rate by divergence state at arm (15m), both eras

| window | floor | JUNE ON | JUNE OFF | JULY ON | JULY OFF |
|---|---|---|---|---|---|
| 5 | 0 | n=29 · **59%** · −94 | n=48 · 48% · −134 | n=187 · **64%** · −402 | n=352 · 64% · +445 |
| 10 | 500 | n=17 · **65%** · +77 | n=60 · 48% · −174 | n=99 · **66%** · −362 | n=440 · 63% · +267 |
| 20 | 500 | n=14 · **64%** · +185 | n=63 · 49% · −186 | n=83 · **70%** · −657 | n=456 · 63% · +299 |
| **30** | **500** | n=15 · **67%** · **+249** | n=62 · 48% · −208 | n=73 · **74%** · **+339** | n=466 · 62% · +122 |
| 50 | 500 | n=11 · 55% · +58 | n=66 · 52% · −148 | n=61 · **70%** · **+571** | n=478 · 63% · +98 |

**The win-rate claim holds.** Divergence-at-arm beats no-divergence on win rate in **both eras at every setting tested** (June +11 to +19pp, July +0 to +11pp). That is the effect the user was looking for, and it survives an era split — it is not a single-period artifact.

**But win rate is not P&L, and at short windows they diverge.** At `window` 10–20 the July cohort wins *more often* (66–70%) while averaging *worse* (−362, −657). Higher hit rate, fatter tail losses. Only at **window ≥ 30** do both eras turn positive on both metrics simultaneously. This is the same shape as §20: frequency does not compensate for tail.

**Settings confirmed:** `cvd_div_window: 30`, `cvd_div_min_delta: 500`, 15m, bar-close. Chosen in the previous session on arm-rate grounds alone (30.4% of bars at default `window=5` → 7.2%); the outcome data now independently picks the same point. Window 50 is comparable in July (+571) but thin and weaker in June (n=11, 55%) — 30 is the more stable choice.

**Do NOT read the direction split as signal.** At W=30 the ON cohort splits bullish n=42 · +763 · 81% vs bearish n=20 · −480 · 60%. With n=20 on one side over a single directional period, this is a market-direction artifact, not evidence that bullish divergences are better. The setup is a neutral straddle; ignore divergence *direction* and use only its *presence*.

**Status:** this converts the CVD-div setup from hypothesis to measured filter — but as a **gate on arming**, which is what was measured. Nothing here tests the proposed opposite-divergence *exit*; that remains unvalidated and is still a basket flatten (§17).

---

## 22. Adaptive grid step by market speed — proposal is half right, and backwards on the half that matters

**Proposal:** widen the step in slow markets, tighten it in fast markets.

612 cycles with a derivable realized step (median gap between adjacent same-side leg prices), each tagged with ATR at arm time and bucketed into SLOW/MID/FAST terciles *within its own TF* (so the regime label means "fast for this TF", not "fast vs 1m").

### Absolute step vs volatility regime

| regime | step | n | avg $ | win% | med legs |
|---|---|---|---|---|---|
| SLOW | <1.0 | 98 | −77 | 60% | 8 |
| SLOW | 1.0–2.0 | 70 | **+469** | 61% | 4 |
| SLOW | 2.0–3.5 | 28 | **+560** | 64% | 4 |
| SLOW | >3.5 | 9 | +194 | 67% | 3 |
| MID | <1.0 | 53 | +83 | 68% | 6 |
| MID | 1.0–2.0 | 95 | −68 | 63% | 5 |
| MID | 2.0–3.5 | 38 | +387 | 61% | 4 |
| MID | >3.5 | 19 | +993 | 68% | 4 |
| FAST | <1.0 | 32 | **−1,330** | 53% | 6 |
| FAST | 1.0–2.0 | 62 | +116 | 61% | 6 |
| FAST | 2.0–3.5 | 73 | **+1,001** | 70% | 4 |
| FAST | >3.5 | 35 | −1,045 | 54% | 3 |

**The "tighter on fast" half is contradicted by the data.** FAST + tight step is the single **worst** cell in the table (−1,330, 53% win, n=32); FAST + $2.0–3.5 is the single **best** (+1,001, 70% win, n=73). Tightening into a fast market is exactly the wrong move — it fills more legs (median 6 vs 4) into a run that keeps going, building inventory against the move.

**The "wider on slow" half is directionally supported but for a different reason.** SLOW improves from −77 (sub-$1) to +469/+560 ($1–3.5). But so does every other regime. The pattern is not *relative* to speed at all:

> **In all three regimes, sub-$1.00 step is the worst or near-worst bucket, and $2.0–3.5 is good. This is an absolute floor, not a volatility-relative rule.**

That floor is consistent with the transaction-cost result in §19 — $0.56/oz round trip friction — which is an absolute dollar quantity and does not scale with ATR. A volatility-proportional step keeps re-crossing that fixed floor; an absolute floor is the correct shape of the fix.

**Regime sensitivity is a TF property, not a step property.** Holding strat×TF constant (same step rule), 15m is flat across regimes while 1m collapses:

| strat\|tf | SLOW | MID | FAST |
|---|---|---|---|
| hvn\|15m | +1,054 (medStep 1.33) | +1,009 (2.40) | +1,003 (2.74) |
| hvn\|5m | +108 (0.99) | +968 (1.47) | +245 (2.14) |
| hvn\|1m | +638 (0.60) | −30 (1.01) | −538 (1.46) |

15m earns ~$1,000/cycle regardless of speed. 1m degrades monotonically as speed rises, because even in FAST its ATR-proportional step only reaches $1.46 — still under the sweet spot. 1m's problem is not that the step fails to adapt; it is that the step never gets large enough in absolute terms.

**Verdict:** do **not** build inverse-ATR adaptive spacing. The mechanism that would pay is a **hard absolute floor on `step`** (~$2.00), applied in all regimes, which mostly binds on 1m/5m and rarely on 15m. Note this is inseparable from the "prefer 15m" conclusion (§7, §19) — a $2 floor on 1m produces a ladder spanning $12 at n=6, wider than most 1m nodes, which is another way of saying 1m should not run this strategy. **Not applied — one change at a time; `lvn_edge_touch` is still under measurement (§19).**

**Caveats.** Step is *derived* from realized fill prices, so cycles whose ladder never filled both sides contribute fewer gaps; buckets are unequal (FAST >3.5 n=35 vs SLOW >3.5 n=9); and step correlates with strategy (candle_sweep runs tight, hvn 15m runs wide), so part of every step effect is a strategy effect. The FAST-tight vs FAST-mid contrast is the most robust single comparison here (n=32 vs n=73, opposite signs, 17pp win-rate gap).

---

## 23. CORRECTION to §21 — the CVD gate does not survive setup selection

§21 measured the CVD-divergence gate **pooled across all 616 cycles** and found a consistent win-rate lift. That result is real but **misleading**, and the conclusion drawn from it was wrong.

Re-run against only the profitable setups:

| strat\|tf | cohort | cyc | avg $ | win% |
|---|---|---|---|---|
| hvn\|15m | ALL | 82 | **+1,058** | 67% |
| hvn\|15m | div ON | 11 | +457 | 64% |
| hvn\|15m | div OFF | 71 | **+1,151** | 68% |
| lvn_edge\|15m | ALL | 20 | +1,007 | 65% |
| lvn_edge\|15m | div ON | 5 | **−607** | 60% |
| lvn_edge\|15m | div OFF | 15 | **+1,545** | 67% |
| hvn\|5m | div ON | 17 | +751 | **88%** |
| hvn\|5m | div OFF | 121 | +387 | 66% |

Portfolio level, keeper set only: **ungated 264 cyc · avg +654 · 68% win** vs **CVD-gated 36 cyc · avg +419 · 75% win.**

**The gate's pooled lift came from excluding the bad setups (1m, candle_sweep), not from improving the good ones.** Setup selection already does that job, and does it better. Applied on top of a clean setup list the gate *subtracts* — it removes 86% of cycles to buy 7pp of win rate at a third less profit per cycle, and on `hvn|15m` and `lvn_edge|15m` it actively strips the best cycles out.

**Gate and setup-selection are substitutes, not complements.** Ship setup selection; drop the gate.

**Consequence for the proposed CVD-div straddle setup:** its main remaining justification is gone. The only surviving signal is `hvn|5m` div-ON at 88% win / +751 (n=17) — too thin to build on, and it is a *gate on an existing profitable setup*, not a standalone trigger. **Do not build the standalone CVD-div setup.** Revisit only if `hvn|5m` div-ON holds up past ~50 cycles live.

**Method lesson (generalizes):** any filter evaluated on a universe containing known-bad cohorts will look good by proxying for "not the bad cohort". Always re-test filters *after* removing the setups you already intend to cut.

---

## 24. How the CVD gate was scored, and what changes if you score it post-close

**How §21/§23 computed it.** For each cycle, take its arm timestamp, find the **last 15m bar that had already CLOSED at or before that instant**, and run `delta_divergence.detect(bar, prior_30_bars, window=30)`, firing only when `delta_vs_window >= 500`. Strictly causal — nothing after the arm is visible. Call this **PRIOR**.

**The objection is valid.** Live history ran `touch_only: true` from 07-07, so many cycles armed **intrabar**. For those, PRIOR can be up to 15 minutes stale — it scores a bar that closed before the move that caused the arm. The fairer comparison scores the bar the arm falls *inside*, known only at **its** close. Call this **CONTAINING**. It cannot gate an intrabar arm (the information does not exist yet), but it can drive a post-arm confirm/kill, so it is worth measuring.

| cohort | PRIOR ON | PRIOR OFF | CONTAINING ON | CONTAINING OFF |
|---|---|---|---|---|
| keeper set | n=36 · +419 · 75% | n=228 · +692 · 67% | n=46 · +694 · 70% | n=218 · +646 · 67% |
| hvn\|15m | n=11 · +457 · 64% | n=71 · **+1,151** · 68% | n=17 · +882 · 65% | n=65 · +1,104 · 68% |
| hvn\|5m | n=17 · +751 · 88% | n=121 · +387 · 66% | n=19 · +476 · 74% | n=119 · +425 · 68% |
| lvn_edge\|15m | n=5 · −607 · 60% | n=15 · **+1,545** · 67% | n=5 · +451 · 60% | n=15 · +1,192 · 67% |

**Timing was a real part of the problem, and fixing it does not rescue the gate.** Post-close scoring lifts the keeper set from +419 to +694 — so PRIOR *was* unfairly penalising the divergence cohort. But CONTAINING ON (+694) vs CONTAINING OFF (+646) is a 7% difference on n=46. That is noise, not an edge.

On the two best cells the gate is still negative-to-neutral under both scorings: `hvn|15m` ON never beats OFF, `lvn_edge|15m` ON never beats OFF. The `hvn|5m` 88% under PRIOR does not replicate under CONTAINING (74%, +476) — which makes it look like a small-sample artifact rather than the one real signal.

**§23's verdict stands, now for a better reason.** It is not that the gate was mis-timed; corrected timing simply shows no edge on a clean setup list. **Do not ship the CVD gate. Do not build the standalone CVD-div setup.**

---

## 25. Squeeze / expansion state at arm — no gate in either direction earns its keep

BBW percentile rank recomputed offline with `squeeze_gate`'s exact math (BB period 20, 3σ, trailing window 100), evaluated on the last closed bar of the cycle's **own** TF at arm time. `rank ≤ 0.15` = coiled (what the old `require_squeeze_gate` demanded); high rank = expanding.

### Keeper set by band

| band | cyc | net | avg | win% |
|---|---|---|---|---|
| COILED ≤.15 | 53 | +37,727 | +712 | **58%** |
| .15–.40 | 48 | +30,722 | +640 | 54% |
| .40–.60 | 37 | +37,675 | **+1,018** | **92%** |
| EXPANDING >.60 | 126 | +66,633 | +529 | 70% |

### Every gate you could build from this loses money

| rule | cyc | net | avg | win% |
|---|---|---|---|---|
| **ungated** | 264 | **+172,757** | +654 | 68% |
| require NOT coiled (>.15) | 211 | +135,030 | +640 | 70% |
| require expanding (>.60) | 126 | +66,633 | +529 | 70% |
| require coiled ≤.15 (the old gate) | 53 | +37,727 | +712 | 58% |

**Requiring expansion does not help.** It buys +2pp of win rate by discarding 138 cycles and **61% of total profit**, and it *lowers* average P&L per cycle (529 vs 654). Every variant is worse than no gate.

**It does retire the old gate for good.** `require_squeeze_gate: true` demanded the coiled band, which has the **worst win rate in the table** (58% vs 70% expanding). Disabling it on 07-09 was correct; this is the cleaner confirmation. Keep `require_squeeze_gate: false`.

### The 92% band is not actionable — do not act on it

The `.40–.60` cell looks spectacular and two adjacent deciles agree (0.4–0.5: 88%, 0.5–0.6: 93%). It still fails on two counts:

1. **One era only.** June has **n=1** in that band; the entire result is July, n=38.
2. **P&L does not follow.** Across deciles the averages are 1,696 · −549 · 1,793 · −119 · 1,067 · 668 · 1,027 · 267 · 122 · 655 — no trend at all. The win-rate lift comes from *smaller wins*, not better ones (+1,018 vs ungated +654 is modest for a 92% hit rate).

Gating to `.40–.60` would keep 39 of 264 cycles and forfeit ~$137k of realised profit to buy per-cycle quality. **Logged as an observation, not a rule.** Re-check if it survives a second era live.

**General result, consistent with §22 and §23:** every filter tried against this system — squeeze, expansion, CVD-divergence — improves win rate and reduces total profit, because the cycles they remove are profitable ones. The only filter that has ever raised *both* is **setup selection** (drop 1m, `candle_sweep`, `hvn_edge|5m`).

---

## 26. Broker-side disaster stop — IMPLEMENTED 2026-08-02

Closes the gap first flagged 2026-07-20 and sized in §18. Three changes:

1. **`config/settings.yaml`** → `grid_levels.disaster_sl_usd: 20.0` (0 disables).
2. **`execution/exec_bridge.py`** → `_disaster_sl_usd()` (cached read, mirrors `_modify_cooldown_s`) and a `_leg_sl()` helper in `enqueue_grid_plan`. Every leg now places with `sl = leg.price ∓ 20.0`, measured from **its own entry** — not a shared per-side level, which would have given the near leg $20 and the outer leg $20 + ladder span. A structural SL (`candle_sweep`'s sweep extreme, displacement's BB mid) still wins where one exists: it is deliberate and tighter, and fires first.
3. **All 5 `mql5/FBExecBridge*.mq5` → v1.10** — *required*, not cosmetic. `ExecModifyPending` called `trade.OrderModify(ticket, price, 0.0, tp, …)`, hardcoding SL to zero. **Every fulcrum shift and every pending TP refresh silently wiped the stop off all legs**, so a placement-time SL alone would have survived only until the first re-anchor. The SL now rides the shift: `useSl = curSl > 0 ? curSl + priceDelta : 0`, matching the ladder's rigid translation so the distance from entry stays constant.

Verified: 6-leg straddle emits `dist 20.00` on all legs both sides; `candle_sweep` still emits its structural 3999/4001.

Position-modify was already safe — `enqueue_modify_position` passes `sl=0` meaning "EA keeps `posInfo.StopLoss()`", so TP refreshes on filled positions preserve the stop.

**Interaction with `defer_sl_on_half_fill: false`.** That flag stays off. It armed a *server-side* SL only after >50% of a side filled — useless during the freeze this is meant to survive, and it fired at the node edge (a strategy stop). The disaster stop is orthogonal: broker-native, present from the moment the leg fills, and far enough out that it should never fire in normal operation.

**Unchanged limit (§18):** this is not gap protection. Jul14 was +63pt in one minute; a $20 stop triggers inside the gap and fills at the far side. Only position size defends against that.

---

## 27. TP-hit counts, and the squeeze→release transition — the first filter that raises BOTH win rate and profit

### TP hits (new measurement)

Per-position TP hit detected directly from the broker Positions table: `|close_price − T/P| ≤ 0.10`. Across 5,865 deduped positions:

- **97.4%** had a TP set (5,714) — **11.1%** of those actually hit it (633).
- SL was set on only 991 (16.9%), hit on 92 — the asymmetry the §26 disaster stop closes.
- Sanity check against §17: 633 hits × ~$452 average ≈ the +286,096 attributed to `structural_TP`. Consistent.

| cohort (keeper set) | cyc | legs | TP hits | TP hit % | avg/cyc | win% | $/TP hit |
|---|---|---|---|---|---|---|---|
| ALL | 264 | 1,957 | 130 | 6.9% | +654 | 68% | 445 |
| CVD div ON at arm | 36 | 239 | 17 | **7.3%** | +419 | 75% | 509 |
| CVD div OFF | 228 | 1,718 | 113 | **6.8%** | +692 | 67% | 435 |
| BBW COILED ≤.15 | 53 | 538 | 23 | **4.3%** | +712 | 58% | **694** |
| BBW mid .15–.60 | 85 | 652 | 52 | **8.5%** | +805 | 71% | 429 |
| BBW EXPANDING >.60 | 126 | 767 | 55 | 7.4% | +529 | 70% | 355 |

**CVD-div makes no difference to TP hits** (7.3% vs 6.8%) — a third independent confirmation of §23/§24. Closed.

**Coiled arms hit TP least but pay most per hit** (4.3% at $694 vs expanding 7.4% at $355). A coil produces few resolutions; the ones it produces are large. That is the squeeze thesis, and it sets up the next result.

### Armed in the squeeze (FBSqueeze green line), exited after release

Green line = `BBW ≤ bottom-15% of trailing 100` — identical to `rank ≤ 0.15`, so the pane and this analysis agree by construction.

First cut, measuring BBW state at the cycle's **last leg close**:

| keeper set, armed GREEN | cyc | avg/cyc | win% | $/TP hit |
|---|---|---|---|---|
| exit still GREEN | 28 | +237 | 61% | 363 |
| exit RELEASED >.15 | 25 | **+1,244** | 56% | 907 |

**That first cut is confounded and must not be quoted.** Still-green cycles had median duration 4.4 bars vs 8.9 for released — and duration alone is strongly predictive (keeper set: <8 bars +837/72%, 8–30 +397/61%, ≥30 −283/50%). Worse, duration is partly tautological: a cycle ends quickly *because* it hit its target.

### The clean test — fixed horizon from the arm

Ask instead: **did BBW release within H bars of the arm bar?** Fixed window, measured forward from arm, completely independent of when the cycle exited.

| | keeper set | | | all 616 | | |
|---|---|---|---|---|---|---|
| **horizon** | **cyc** | **avg/cyc** | **win%** | **cyc** | **avg/cyc** | **win%** |
| released ≤5 bars | 24 | **+1,258** | **67%** | 63 | **+672** | **62%** |
| stayed coiled 5 bars | 29 | +260 | 52% | 51 | +19 | 47% |
| released ≤10 bars | 44 | **+1,208** | **61%** | 93 | **+679** | **59%** |
| stayed coiled 10 bars | 9 | **−1,712** | 44% | 21 | **−946** | 38% |

**The effect survives the confound control, in both samples, at both horizons.** This is the **only** filter found in this entire analysis that improves win rate *and* profit at the same time (§25: squeeze, expansion, and CVD all raised win% while cutting total profit). Every other filter removed profitable cycles; this one removes cycles that genuinely lose.

The coiled-10-bar cohort hit **zero** TPs across all 9 keeper cycles (0.0%) — nothing resolved, because nothing moved.

**The original squeeze thesis was right; the gate expressed it backwards.** `require_squeeze_gate: true` demanded "arm while coiled" — but coil-that-stays-coiled earns +260 → −1,712. The tradeable object was never the coil, it is the **release**. FBSqueeze's own header says so: the ▲ arrow is "the expansion the straddle plays."

### What is actually actionable

Not an arm gate — you cannot know at arm time whether the coil will break. It is a **kill rule**:

> A cycle armed while BBW is compressed (`rank ≤ 0.15`) that has **not** released within ~10 bars is worth **−1,712/cycle** (keeper) / −946 (all 616), wins 38–44% of the time, and hits no TPs. Close it.

This is a flatten, and §17 established flattens as the −866,070 loss driver — but that finding was about *indiscriminate* flattening of cycles that were still working. This is targeted at a cohort measured to be negative. The two are not in conflict; distinguishing them is the point.

**Caveats.** n=9 (keeper) / n=21 (all 616) in the kill cohort — small, though consistent in sign and magnitude across both samples and both horizons. Base rate is low: only 53 of 264 keeper cycles arm while coiled, and only 9 of those fail to release in 10 bars, so this fires perhaps twice a month. **Not implemented — logged for a single-change slot after `lvn_edge_touch` and the 5m addition.**
