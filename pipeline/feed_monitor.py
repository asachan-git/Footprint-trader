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
# symbol -> close_ts of the last healthy bar seen just before it went stale (the gap's left
# edge). Captured on the ok→stale transition, consumed on stale→ok to backfill the hole.
_gap_start: dict[str, int] = {}
_lock = threading.Lock()

# Callback set by start(): (symbol, gap_start_ts, now_ts) -> None. Fires the aggTrades
# self-heal on a stale→ok recovery. None when auto-backfill is disabled or unconfigured.
_on_recover = None
# Settings snapshot set by start() — lets check_once reach monitor.* config (feed-hedge) with
# no signature change (existing check_once unit tests pass symbols/tf/gap only). None until start.
_settings = None


def health() -> dict[str, dict]:
    """Snapshot of per-symbol feed health (for the dashboard / health route)."""
    with _lock:
        return {k: dict(v) for k, v in _health.items()}


def check_once(symbols: list[str], tf: str, gap_alert_secs: float,
               now: float | None = None) -> list[dict]:
    """One health pass. Updates `_health`, logs WARN on stall / INFO on recovery, fires the
    backfill callback (if set) on recovery, and returns the symbols that transitioned."""
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
            # Capture the gap's left edge on the ok→stale edge: the newest bar we HAD then
            # is the last good one; everything after it is the hole to heal on recovery.
            if status == "stale" and prev != "stale" and last_ts:
                _gap_start[sym] = int(last_ts)
            gap_start = _gap_start.get(sym)
        # Protective feed-hedge: on EVERY stale pass count toward the hedge threshold; the
        # module opens the hedge once at the threshold and no-ops thereafter. Gated + fail-open
        # inside feed_hedge. Runs regardless of transition (needs the per-pass count).
        if _settings is not None and status == "stale":
            try:
                from pipeline import feed_hedge
                feed_hedge.on_stale(sym, tf, _settings)
            except Exception as e:
                LOG.debug(f"[gap-monitor] {sym} hedge on_stale skipped: {e}")
        if status != prev:
            transitions.append({"symbol": sym, "status": status, "age_s": age})
            if status == "stale":
                LOG.warning(f"[gap-monitor] {sym} {tf} feed STALE — no bar for "
                            f"{age:.0f}s (threshold {gap_alert_secs:.0f}s)")
            elif prev is not None:
                LOG.info(f"[gap-monitor] {sym} {tf} feed RECOVERED — fresh bar after stall")
                # Self-heal: backfill the missed [gap_start, now] window (non-blocking).
                if _on_recover is not None and gap_start:
                    try:
                        _on_recover(sym, gap_start, int(now))
                    except Exception as e:
                        LOG.warning(f"[gap-monitor] {sym} backfill trigger failed: {e}")
                with _lock:
                    _gap_start.pop(sym, None)
                # Remove the protective hedge now that data is back.
                if _settings is not None:
                    try:
                        from pipeline import feed_hedge
                        feed_hedge.on_recovered(sym, tf, _settings)
                    except Exception as e:
                        LOG.debug(f"[gap-monitor] {sym} hedge on_recovered skipped: {e}")
    return transitions


# store_symbol → (binance_fetch_symbol, price_step). Mirrors the feed launch args in
# scripts/start.sh (--symbol/--symbol-as/--price-step). Overridable via
# settings.monitor.backfill_feeds. XAU is fetched from Binance as XAUUSDT but stored as
# XAUTUSDT (the analysis key); BTC is 1:1.
_DEFAULT_BACKFILL_FEEDS = {
    "XAUTUSDT": {"binance_symbol": "XAUUSDT", "price_step": 0.1},
    "BTCUSDT":  {"binance_symbol": "BTCUSDT", "price_step": 1.0},
}


def _make_recover_cb(settings: dict, tf: str, gap_alert_secs: float):
    """Build the stale→ok recovery callback that self-heals the missed window via aggTrades.
    Returns None if auto-backfill is disabled. The callback runs the fetch on a daemon thread
    so a multi-hour heal never blocks the monitor loop."""
    mon = settings.get("monitor", {}) or {}
    if not mon.get("gap_backfill_enabled", True):
        LOG.info("[gap-monitor] auto-backfill disabled via settings.monitor.gap_backfill_enabled")
        return None
    feeds = {**_DEFAULT_BACKFILL_FEEDS, **(mon.get("backfill_feeds") or {})}
    max_minutes = float(mon.get("max_backfill_minutes", 1440))

    def _cb(store_symbol: str, gap_start_ts: int, now_ts: int) -> None:
        feed = feeds.get(store_symbol)
        if not feed:
            LOG.info(f"[gap-monitor] no backfill feed configured for {store_symbol} — skip heal")
            return
        gap_min = (now_ts - gap_start_ts) / 60.0
        if gap_min <= (gap_alert_secs / 60.0):
            return  # sub-threshold blip; the next live bar covers it
        if gap_min > max_minutes:
            LOG.warning(f"[gap-monitor] {store_symbol} gap {gap_min:.0f}min > "
                        f"max_backfill_minutes {max_minutes:.0f} — skipping auto-heal "
                        f"(cold-start? use scripts/backfill_binance.py)")
            return
        start_ms = (gap_start_ts + 60) * 1000   # first missed minute (gap_start is the last GOOD bar)
        end_ms = now_ts * 1000

        def _do():
            try:
                from binance.backfill import backfill_window
                n = backfill_window(feed["binance_symbol"], store_symbol, start_ms, end_ms,
                                    tf=tf, price_step=float(feed["price_step"]),
                                    source="binance_agg_recover")
                LOG.info(f"[gap-monitor] backfilled {n} {tf} bars for {store_symbol} "
                         f"({gap_min:.0f}min gap, {feed['binance_symbol']} aggTrades)")
            except Exception as e:
                LOG.warning(f"[gap-monitor] {store_symbol} backfill failed: {e}")

        threading.Thread(target=_do, daemon=True,
                         name=f"gap-backfill-{store_symbol}").start()

    return _cb


def start(settings: dict) -> None:
    """Launch the monitor as a daemon thread. No-op if disabled in settings."""
    global _on_recover, _settings
    _settings = settings
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
    _on_recover = _make_recover_cb(settings, tf, gap_alert)

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
             f"{gap_alert:.0f}s, check every {interval:.0f}s, "
             f"auto-backfill={'on' if _on_recover else 'off'}")
