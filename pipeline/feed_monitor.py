"""Feed-gap monitor — warns when bar ingestion stalls.

Background watcher that checks, per symbol, how long since the last primary-TF
bar landed in the state_store. When the age crosses `gap_alert_secs` it logs a
WARNING once (and again on recovery), so a stalled/dead feed or an ingest outage
is noticed in seconds instead of being discovered hours later as a chart gap.

Caveat: this runs INSIDE the server, so it catches feed-side stalls (producer
died, exchange outage, /ingest erroring) but NOT a full server-down event — for
that you need external monitoring. The 2026-06-05 16:26→17:06 IST gap was a
full-stack stop, which this wouldn't have caught; it does catch the more common
case where the server is up but bars stop arriving.

Health is published to a module global so a dashboard route can surface a banner.
"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger(__name__)

# symbol -> {"last_bar_ts": int, "age_s": float, "status": "ok"|"stale", "since": int}
_health: dict[str, dict] = {}
_lock = threading.Lock()


def health() -> dict[str, dict]:
    """Snapshot of per-symbol feed health (for the dashboard / health route)."""
    with _lock:
        return {k: dict(v) for k, v in _health.items()}


def check_once(symbols: list[str], tf: str, gap_alert_secs: float,
               now: float | None = None) -> list[dict]:
    """One health pass. Updates `_health`, logs WARN on stall / INFO on recovery,
    and returns the list of symbols that transitioned this pass."""
    from pipeline.state_store import store
    now = time.time() if now is None else now
    s = store()
    transitions: list[dict] = []
    for sym in symbols:
        recent = s.recent(sym, tf, 1)
        last_ts = recent[-1].close_ts if recent else 0
        age = now - last_ts if last_ts else float("inf")
        status = "stale" if age > gap_alert_secs else "ok"
        with _lock:
            prev = _health.get(sym, {}).get("status")
            _health[sym] = {"last_bar_ts": last_ts, "age_s": round(age, 1),
                            "status": status, "since": int(now)}
        if status != prev:
            transitions.append({"symbol": sym, "status": status, "age_s": age})
            if status == "stale":
                LOG.warning(f"[gap-monitor] {sym} {tf} feed STALE — no bar for "
                            f"{age:.0f}s (threshold {gap_alert_secs:.0f}s)")
            elif prev is not None:
                LOG.info(f"[gap-monitor] {sym} {tf} feed RECOVERED — fresh bar after stall")
    return transitions


def start(settings: dict) -> None:
    """Launch the monitor as a daemon thread. No-op if disabled in settings."""
    mon = settings.get("monitor", {}) or {}
    if not mon.get("gap_monitor_enabled", True):
        LOG.info("[gap-monitor] disabled via settings.monitor.gap_monitor_enabled")
        return
    tf = settings["instrument"]["primary_tf"]
    vp_cfg = settings.get("vp_cache", {})
    symbols = vp_cfg.get("symbols", [settings["instrument"]["symbol"]])
    # 1m feed → a missed bar shows within ~1 bar; default alert at 3 bars of silence.
    tf_secs = {"1m": 60, "5m": 300, "15m": 900}.get(tf, 60)
    gap_alert = float(mon.get("gap_alert_secs", 3 * tf_secs))
    interval = float(mon.get("gap_check_secs", max(30, tf_secs / 2)))

    def _run():
        # Grace period so a fresh start (mid-backfill) doesn't false-alarm.
        time.sleep(interval)
        while True:
            try:
                check_once(symbols, tf, gap_alert)
            except Exception as e:
                LOG.debug(f"[gap-monitor] check failed: {e}")
            time.sleep(interval)

    threading.Thread(target=_run, daemon=True, name="gap-monitor").start()
    LOG.info(f"[gap-monitor] watching {symbols} {tf} — alert if no bar for "
             f"{gap_alert:.0f}s, check every {interval:.0f}s")
