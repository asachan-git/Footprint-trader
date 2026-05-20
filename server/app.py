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
from .routes.replay import bp as replay_bp
from .routes.decision import bp as decision_bp
from .routes.decide import bp as decide_bp
from .routes.analyze import bp as analyze_bp
from .routes.label import bp as label_bp
from .routes.stats import bp as stats_bp
from .routes.footprint_view import bp as footprint_bp
from .routes.decide_multi import bp as decide_multi_bp
from .routes.heatmap import bp as heatmap_bp

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
    primary_tf = settings["instrument"]["primary_tf"]
    LOG.info(f"[startup] pre-computing VP cache for {symbols} (session_start={session_start}, bin_size={bin_size_cfg})...")
    try:
        build_and_save(
            list(set(symbols)), primary_tf,
            session_start_utc=session_start,
            vp_bin_size=bin_size_cfg,
        )
    except Exception as e:
        LOG.warning(f"[startup] VP cache build failed (non-fatal): {e}")


def create_app() -> Flask:
    load_dotenv(ROOT / ".env")
    static_dir = str(ROOT / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    settings = load_settings()
    app.config["FB_SETTINGS"] = settings
    _precompute_vp(settings)
    app.register_blueprint(health_bp)
    app.register_blueprint(ingest_bp)
    app.register_blueprint(replay_bp)
    app.register_blueprint(decision_bp)
    app.register_blueprint(decide_bp)
    app.register_blueprint(analyze_bp)
    app.register_blueprint(label_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(footprint_bp)
    app.register_blueprint(decide_multi_bp)
    app.register_blueprint(heatmap_bp)
    return app


def main() -> None:
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
