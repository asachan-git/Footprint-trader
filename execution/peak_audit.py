"""Peak-tracking audit — independently re-verifies sidefull_trail/bias_trail's own peak.

Background watchdog, same pattern as pipeline/feed_monitor.py. Runs a SEPARATE broker
query on its own cadence and maintains its own shadow peak per (account, broker_symbol,
magic), independent of monitor_cycle's sidefull_peak/bias_peak (execution/exec_bridge.py).
If the shadow peak drifts meaningfully above the recorded one, logs a loud alert.

Why this exists (2026-08-07): magic 776132's recorded sidefull_peak was $128 while its
true peak (reconstructed by hand from broker price data) was $1,018.75 — the trail's
giveback threshold was computed off a stale base and never fired where it should have.
Root cause (EA self-reported buy_pnl/sell_pnl going stale) was fixed at the source in
server/routes/exec_bridge.py's _merge_broker_magics — broker truth now wins
unconditionally on every poll. This module is the second layer: ongoing, independent
verification that the fix keeps holding, catching this class of bug even if it comes
from a different cause next time (a regression in monitor_cycle's own peak-max logic, a
timing race, anything not already covered by the bridge-outage alert in exec_bridge.py).

Alert-only by design (2026-08-07, user) — does not mutate arm state. A recurring alert is
itself the signal to investigate directly, the same way tonight's incident was found.
"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger(__name__)

# (account, broker_symbol, magic) -> shadow peak ($, net buy_pnl+sell_pnl), independent of
# the arm's own sidefull_peak/bias_peak.
_AUDIT_PEAK: dict[tuple[str, str, int], float] = {}
# same key -> arm `ts` last seen, to detect a new cycle (re-arm) and reset the shadow peak —
# comparing across two different cycles on the same magic is meaningless.
_AUDIT_ARM_TS: dict[tuple[str, str, int], float] = {}
# same key -> consecutive-divergence counter, so a single-tick race (this check's broker
# read landing a beat before monitor_cycle's own update) doesn't false-alarm.
_AUDIT_STREAK: dict[tuple[str, str, int], int] = {}
_LOCK = threading.Lock()

# (account, broker_symbol, magic) -> {"recorded": float, "audited": float, "diff": float,
# "since": float} for the most recent DIVERGING magics. Cleared once a magic stops diverging.
_health: dict[tuple[str, str, int], dict] = {}


def health() -> dict[tuple[str, str, int], dict]:
    """Snapshot of currently-diverging magics (for a future dashboard route)."""
    with _LOCK:
        return {k: dict(v) for k, v in _health.items()}


def check_once(settings: dict, now: float | None = None) -> list[dict]:
    """One audit pass across every account/symbol currently holding an active cycle.
    Returns the list of divergences flagged this pass (empty in the normal case)."""
    from execution.exec_bridge import ExecBridge
    from server.routes.exec_bridge import _broker_magics

    now = time.time() if now is None else now
    grid_cfg = (settings or {}).get("grid_levels", {}) or {}
    mon_cfg = (settings or {}).get("monitor", {}) or {}
    tol_usd = float(mon_cfg.get("peak_audit_tolerance_usd", 20.0) or 20.0)
    tol_pct = float(mon_cfg.get("peak_audit_tolerance_pct", 0.05) or 0.05)

    flagged: list[dict] = []
    for account, broker_symbol in ExecBridge.active_accounts_symbols():
        cycles = ExecBridge.active_cycles_detail(account, broker_symbol)
        if not cycles:
            continue
        try:
            broker_rows, _, _ = _broker_magics(broker_symbol, {"grid_levels": grid_cfg})
        except Exception:
            LOG.exception("[peak-audit] broker query failed for %s/%s", account, broker_symbol)
            continue
        broker_by_magic = {int(r["magic"]): r for r in broker_rows}

        for cyc in cycles:
            magic = int(cyc["magic"])
            key = (account, broker_symbol, magic)
            arm_ts = float(cyc.get("ts") or 0.0)

            # Cycle boundary: this magic re-armed since we last looked — the shadow peak
            # from the PREVIOUS cycle no longer means anything, start fresh.
            with _LOCK:
                if _AUDIT_ARM_TS.get(key) != arm_ts:
                    _AUDIT_ARM_TS[key] = arm_ts
                    _AUDIT_PEAK[key] = 0.0
                    _AUDIT_STREAK[key] = 0

            br = broker_by_magic.get(magic)
            if br is None:
                continue   # no broker exposure for this magic right now — nothing to audit
            net = float(br.get("buy_pnl", 0.0) or 0.0) + float(br.get("sell_pnl", 0.0) or 0.0)

            with _LOCK:
                shadow = max(_AUDIT_PEAK.get(key, 0.0), net)
                _AUDIT_PEAK[key] = shadow

            recorded = max(float(cyc.get("sidefull_peak") or 0.0), float(cyc.get("bias_peak") or 0.0))
            tolerance = max(tol_usd, tol_pct * shadow)
            diverging = shadow > recorded + tolerance

            with _LOCK:
                if diverging:
                    _AUDIT_STREAK[key] = _AUDIT_STREAK.get(key, 0) + 1
                else:
                    _AUDIT_STREAK[key] = 0
                    _health.pop(key, None)
                streak = _AUDIT_STREAK[key]

            if diverging and streak >= 2:
                diff = shadow - recorded
                entry = {"account": account, "broker_symbol": broker_symbol, "magic": magic,
                         "recorded": round(recorded, 2), "audited": round(shadow, 2),
                         "diff": round(diff, 2), "since": now}
                with _LOCK:
                    _health[key] = entry
                flagged.append(entry)
                LOG.error("[peak-audit] *** PEAK DIVERGENCE magic=%d *** recorded=$%.2f "
                         "audited=$%.2f (diff $%.2f) — trail's giveback threshold is stale. "
                         "Check broker bridge health and monitor_cycle's peak-update path.",
                         magic, recorded, shadow, diff)
    return flagged


def start(settings: dict) -> None:
    """Launch the audit as a daemon thread. No-op if disabled in settings."""
    mon = settings.get("monitor", {}) or {}
    if not mon.get("peak_audit_enabled", True):
        LOG.info("[peak-audit] disabled via settings.monitor.peak_audit_enabled")
        return
    interval = float(mon.get("peak_audit_interval_s", 15.0) or 15.0)
    tol_usd = float(mon.get("peak_audit_tolerance_usd", 20.0) or 20.0)
    tol_pct = float(mon.get("peak_audit_tolerance_pct", 0.05) or 0.05)

    def _run():
        # Grace period so a fresh start (no arms loaded yet) doesn't false-alarm.
        time.sleep(interval)
        while True:
            try:
                check_once(settings)
            except Exception as e:
                LOG.debug(f"[peak-audit] check failed: {e}")
            time.sleep(interval)

    threading.Thread(target=_run, daemon=True, name="peak-audit").start()
    LOG.info(f"[peak-audit] watching active cycles — flag if independently-tracked peak "
             f"exceeds recorded sidefull_peak/bias_peak by > max(${tol_usd:.0f}, "
             f"{tol_pct*100:.0f}%), check every {interval:.0f}s")
