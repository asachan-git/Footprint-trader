# Shadow-Run Merge Gate

**Status:** proposed, not yet run
**Purpose:** decide whether `feat/backtest-seams` is safe to merge into the live trading branch
**Replaces:** replay-based parity (`scripts/parity_check.sh`), which cannot answer this — see below

---

## Why replay parity failed

The original Phase-0 plan gated the merge on: capture a live session's `/exec/poll` bodies,
replay them through old and new code, and diff the emitted command streams. Byte-identical
would prove the clock/paths refactor inert.

It was run on 2026-07-22 against a 977-poll capture (21.8 min). It does not work, for a
structural reason rather than a fixable bug:

**The server is stateful; the capture is a fixed recording.** A poll body carries the LIVE
server's `magics[]` — its open positions. The replay server arms its own cycles, holds no
positions (it acks commands but nothing fills), and therefore never progresses to an exit.
After the first divergence the recorded state describes a world the replay is not in, and
the two drift apart permanently.

Measured, after fixing a genuine bug along the way (the replay never acked, so every command
re-sent forever — 38,758 commands vs live's 223; acking cut it to ~1,038):

| | commands over the same 21.8 min |
|---|---|
| live | 154 legs in 25 arm batches |
| replay | 873 legs — **5.7x over-arming** |

The armed *magic set* matched 12 of 13 (the miss is a `candle_sweep` magic needing venue
bars), so the strategy layer is broadly agreeing — but the volume is not comparable, and no
threshold on that diff would mean anything.

A second attempt compared two checkouts against the same capture, hoping the divergence
would cancel. It does not: base produced 330 commands, Phase-0 produced 1,023. That number is
**confounded, not evidence of a defect** — the base checkout predates `paths.py` and resolves
data paths differently, so the two runs were not doing the same I/O.

`scripts/parity_replay.py` is still useful for comparing two checkouts under *identical*
conditions, and its docstring now says so. It is not a merge gate.

---

## What the merge actually needs to establish

Only one claim: **the Phase-0 seams change no live behaviour.** They are supposed to be
inert — `clock.now()` is `time.time()` when no source is installed, and the path functions
resolve to the same files when `FB_DATA_DIR` is unset.

Evidence already in hand (necessary, not sufficient):

- 66/66 tests pass on every commit
- Default paths verified byte-identical to the pre-refactor constants
- `clock.now()` verified to return wall time, and the derived IST hour matches
- Of 149 non-comment changed lines in the Phase-0 commit, 111 are mechanical
  `time.time()` → `clock.now()` or constant → function swaps; the remainder is the new
  `clock.py` module itself. No trading-logic line was touched.

What is missing is an *empirical* demonstration on live-shaped traffic.

**Complication to be aware of:** three commits on this branch after Phase-0 change live
behaviour deliberately — `b288ef8` (rolling-VP recompute only on a new fractal), `3771450`
(per-side bias-trail one-shot), `4515be0` (transmit side on CANCEL_PENDINGS). Two are not
part of the backtest work. Any old-vs-new comparison that spans them is measuring those
changes, not the seams. The shadow run must therefore compare **branch HEAD vs the live
branch**, and interpret differences in the light of those three commits — or, for a pure
seam test, run `f63eb52` against its own parent.

---

## The shadow run

Run the new code as a **second server instance against a second demo account**, side by side
with the live instance, on the same market. Both see the same prices and the same session;
each drives its own EA. Compare the cycles they produce.

This works precisely where replay failed: the shadow instance has its own real positions, so
its state is self-consistent rather than borrowed from a recording.

### Setup

Existing support (verified):
- `FLASK_PORT` / `FLASK_HOST` are env-configurable (`server/app.py:155-156`)
- `ExecBridge.check_account_switch` already handles a distinct account and retires arms
  belonging to another one
- `data/cycles/cycle_outcomes_*.jsonl` records `account` per row, so the two streams separate
  cleanly
- `FB_DATA_DIR` isolates all writes, so the shadow cannot touch live state

```bash
# shadow instance — new code, second demo account, isolated data dir, different port
FB_DATA_DIR=/path/to/shadow_data \
FLASK_PORT=5001 \
PYTHONPATH=. .venv/bin/python -m server.app
```

Then attach a second MT5 terminal (or a second chart on a second demo login) running
FBExecBridge with `InpBridgeURL = http://127.0.0.1:5001`.

Requirements for the comparison to be meaningful:
- **Same symbol, same session, overlapping wall-clock window.** Start both within a minute
  of each other.
- **Same config.** Diff `config/settings.yaml` between the two checkouts first; any
  difference invalidates the run.
- **Comparable account size.** Lot sizing scales with balance, so a 25k live vs 100k demo
  makes leg lots differ for reasons unrelated to the seams.
- **Long enough to cover a London or NY window.** An Asia-only sample will be too quiet —
  the 2026-07-22 capture produced 25 arm batches in 21.8 min of Asia; a full session is
  needed for the arm counts to be statistically comparable.

### What to compare

Read both `cycle_outcomes_*.jsonl` files and compare on the fields that are
placement-time invariant. **Do not compare `buy_n` / `sell_n`** — `reconcile_from_poll`
overwrites them with live position counts (`execution/exec_bridge.py:1352-1355`), so at exit
they mean "positions still held", not "legs placed". That mistake already cost a day of
misdiagnosis in the backtest work.

| check | field | expectation |
|---|---|---|
| same setups fire | `trigger_kind` × `tf` counts | within noise |
| same anchors | `fulcrum`, `node_low`, `node_high` | near-exact at matched arm times |
| same geometry | `step`, `n_per_side` | near-exact |
| same targets | `tp_up`, `tp_down` | near-exact |
| same exits | `exit_reason` distribution | similar |
| P&L | `pnl_at_exit` | correlated; do NOT expect equality |

Arms will not align one-for-one — two accounts fill at different moments and diverge from
there, exactly as two live accounts would. **Judge the distributions, not individual rows.**
A seam defect would show as a *systematic* difference: a whole `trigger_kind` missing, arm
counts off by a large factor, fulcrums offset, or geometry differing in one direction.

### Pass criteria

- No `trigger_kind` present in one stream and absent in the other
- Arm counts per `kind × tf` within ~2x, and not systematically one-sided
- `fulcrum` / `node_low` / `node_high` distributions overlapping
- `step` and `n_per_side` distributions overlapping
- No new `exit_reason` appearing only in the shadow
- No unexplained error or exception in the shadow log

Fail on any systematic divergence; investigate before merging.

---

## Cheaper alternative, if a second demo account is not available

Run the shadow instance in **dry-run** mode: same new code, same live poll traffic, but its
commands are logged instead of executed. This still exercises the full arm→plan→command path
under real state, and the *arms* remain comparable even though fills do not exist.

That is weaker than the two-account version — no fill-driven lifecycle, so exits and P&L are
untestable — but it is strictly better than replay, because the instance sees live prices in
real time rather than reading a fixed recording.

---

## Recommendation

Given the seams are provably mechanical by inspection and pass the full test suite, the
residual risk is low but not zero. A single London-session shadow run would close it.

Until it is run, the honest status is: **Phase-0 merge gate unmet — evidence is strong by
inspection, absent by measurement.**
