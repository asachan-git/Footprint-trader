"""Flask app factory.

Loads settings.yaml + .env, registers blueprints, runs.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from utils.logging_config import setup as _setup_logging
_setup_logging()
from flask import Flask

from .routes.health import bp as health_bp
from .routes.ingest import bp as ingest_bp
from .routes.decide import bp as decide_bp
from .routes.footprint_view import bp as footprint_bp
from .routes.decide_multi import bp as decide_multi_bp
from .routes.grid_tick import bp as grid_tick_bp
from .routes.grid_levels import bp as grid_levels_bp
from .routes.exec_bridge import bp as exec_bridge_bp
from .routes.heatmap import bp as heatmap_bp
from .routes.options_ingest import bp as options_ingest_bp
from .routes.options_decide import bp as options_decide_bp
from .routes.dashboard import bp as dashboard_bp
from .routes.strategies import bp as strategies_bp

ROOT = Path(__file__).resolve().parent.parent


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def _precompute_vp(settings: dict) -> None:
    """Pre-compute VP cache on startup. Runs synchronously before serving requests."""
    import logging
    from pipeline.features.vp_cache import build_and_save
    LOG = logging.getLogger(__name__)
    vp_cfg = settings.get("vp_cache", {})
    symbols = vp_cfg.get("symbols", [settings["instrument"]["symbol"]])
    session_start = vp_cfg.get("session_start_utc", {})
    bin_size_cfg = vp_cfg.get("vp_bin_size", {})
    offset_cfg = vp_cfg.get("venue_price_offset", {})
    sym_map = (settings.get("execution") or {}).get("symbol_map", {})
    primary_tf = settings["instrument"]["primary_tf"]
    LOG.info(f"[startup] pre-computing VP cache for {symbols} (session_start={session_start}, bin_size={bin_size_cfg}, offset={offset_cfg})...")
    try:
        build_and_save(
            list(set(symbols)), primary_tf,
            session_start_utc=session_start,
            vp_bin_size=bin_size_cfg,
            venue_price_offset=offset_cfg,
            symbol_map=sym_map,
        )
    except Exception as e:
        LOG.warning(f"[startup] VP cache build failed (non-fatal): {e}")

    # Iteration observation snapshot
    try:
        from execution.observation_logger import write_snapshot
        snap = write_snapshot(tag="startup")
        LOG.info(f"[startup] observation snapshot written: {snap.name}")
    except Exception as e:
        LOG.warning(f"[startup] observation snapshot failed (non-fatal): {e}")


def _backfill_sweeps(settings: dict) -> None:
    """Replay last 5 days of bars through sweep detector on startup."""
    import logging
    import threading
    LOG = logging.getLogger(__name__)
    vp_cfg = settings.get("vp_cache", {})
    symbols = vp_cfg.get("symbols", [settings["instrument"]["symbol"]])
    primary_tf = settings["instrument"]["primary_tf"]

    # Backfill both primary TF and 15m so dashboard has sweeps at both granularities
    extra_tfs = ["15m"] if primary_tf != "15m" else []

    def _run():
        from pipeline.features.sweep import backfill_registry
        for sym in symbols:
            for tf in [primary_tf] + extra_tfs:
                try:
                    n = backfill_registry(sym, tf, days=5)
                    LOG.info(f"[startup] sweep backfill {sym}/{tf}: {n} sweeps detected")
                except Exception as e:
                    LOG.warning(f"[startup] sweep backfill {sym}/{tf} failed: {e}")

    # Run in background thread so server starts immediately
    threading.Thread(target=_run, daemon=True, name="sweep-backfill").start()


def create_app(settings: dict | None = None, start_background: bool = True) -> Flask:
    """Build the Flask app.

    Args default to the live path — `create_app()` behaves exactly as before.
    The backtest harness passes `settings=<replay cfg>, start_background=False` to
    inject its config and suppress every startup side effect that would either build
    against live VP or spawn a thread that reads wall-clock feeds:
      - _precompute_vp / assert_ready  (harness supplies point-in-time VP itself)
      - _backfill_sweeps thread
      - feed-gap monitor thread
    """
    load_dotenv(ROOT / ".env")
    static_dir = str(ROOT / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    if settings is None:
        settings = load_settings()
    app.config["FB_SETTINGS"] = settings
    if start_background:
        _precompute_vp(settings)
        # Preflight: footprint must cover ≥5d and VP/HVN/LVN must be present, else REFUSE
        # to start (no trading on stale/short data). Runs after VP build so it checks the
        # freshly-computed cache. Disable via startup_check.enabled=false (not for live).
        from pipeline.startup_check import assert_ready
        assert_ready(settings)
        _backfill_sweeps(settings)
        try:
            from pipeline.feed_monitor import start as _start_gap_monitor
            _start_gap_monitor(settings)
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning(f"[startup] gap-monitor failed to start (non-fatal): {e}")
        try:
            from execution.peak_audit import start as _start_peak_audit
            _start_peak_audit(settings)
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning(f"[startup] peak-audit failed to start (non-fatal): {e}")
    app.register_blueprint(health_bp)
    app.register_blueprint(ingest_bp)
    app.register_blueprint(decide_bp)
    app.register_blueprint(footprint_bp)
    app.register_blueprint(decide_multi_bp)
    app.register_blueprint(grid_tick_bp)
    app.register_blueprint(grid_levels_bp)
    app.register_blueprint(exec_bridge_bp)
    app.register_blueprint(heatmap_bp)
    app.register_blueprint(options_ingest_bp)
    app.register_blueprint(options_decide_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(strategies_bp)
    # Restore arm/emit state persisted before the last restart so live cycles
    # are not orphaned. Must run after blueprints register (imports ExecBridge).
    try:
        from execution.exec_bridge import ExecBridge
        result = ExecBridge.load_persisted_state()
        import logging as _l
        _l.getLogger(__name__).info(f"[startup] arm state restored: {result}")
    except Exception as e:
        import logging as _l
        _l.getLogger(__name__).warning(f"[startup] arm state restore failed (non-fatal): {e}")
    return app


def main() -> None:
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
