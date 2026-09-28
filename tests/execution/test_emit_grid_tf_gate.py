"""/exec/emit_grid refuses a setup on a TF the config does not enable for it."""
from __future__ import annotations

import flask
import pytest

import server.routes.exec_bridge as route

SETTINGS = {
    "instrument": {"primary_tf": "1m"},
    "grid_levels": {"triggers": [{"kind": "hvn_inside_touch", "tfs": ["15m"]},
                                 {"kind": "candle_sweep", "tfs": ["15m"], "enabled": False}]},
    "execution": {"symbol_map": {"XAUTUSDT": "XAUUSD.pc"}},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FB_EXEC_TOKEN", raising=False)
    app = flask.Flask(__name__)
    app.config["FB_SETTINGS"] = SETTINGS
    app.register_blueprint(route.bp)
    return app.test_client()


@pytest.mark.parametrize("tf", ["1m", "3m", "5m", "10m", "1h"])
def test_hvn_inside_touch_blocked_off_its_tfs(client, tf):
    r = client.post("/exec/emit_grid", json={"account": "a", "symbol": "XAUUSD.pc", "tf": tf,
                                              "trigger_hint": "hvn_inside_touch"}).get_json()
    assert r["verdict"] == "skip" and r["skip_reason"] == f"hvn_inside_touch:{tf}_not_in_tfs"


@pytest.mark.parametrize("hint", ["squeeze", "candle_sweep"])
def test_unlisted_or_disabled_kinds_blocked(client, hint):
    r = client.post("/exec/emit_grid", json={"account": "a", "symbol": "XAUUSD.pc", "tf": "15m",
                                              "trigger_hint": hint}).get_json()
    assert r["skip_reason"] == f"{hint}:not_in_triggers"


def test_enabled_tf_passes_the_gate(client):
    r = client.post("/exec/emit_grid", json={"account": "a", "symbol": "XAUUSD.pc", "tf": "15m",
                                              "trigger_hint": "hvn_inside_touch"}).get_json()
    assert "not_in_tfs" not in str(r.get("skip_reason", ""))
