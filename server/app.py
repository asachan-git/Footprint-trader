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
from .routes.heatmap import bp as heatmap_bp
from .routes.options_ingest import bp as options_ingest_bp
from .routes.options_decide import bp as options_decide_bp
from .routes.dashboard import bp as dashboard_bp

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


def create_app() -> Flask:
    load_dotenv(ROOT / ".env")
    static_dir = str(ROOT / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    settings = load_settings()
    app.config["FB_SETTINGS"] = settings
    _precompute_vp(settings)
    app.register_blueprint(health_bp)
    app.register_blueprint(ingest_bp)
    app.register_blueprint(decide_bp)
    app.register_blueprint(footprint_bp)
    app.register_blueprint(decide_multi_bp)
    app.register_blueprint(grid_tick_bp)
    app.register_blueprint(heatmap_bp)
    app.register_blueprint(options_ingest_bp)
    app.register_blueprint(options_decide_bp)
    app.register_blueprint(dashboard_bp)
    return app


def main() -> None:
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
