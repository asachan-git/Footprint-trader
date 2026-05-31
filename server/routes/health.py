"""Health route — subsystem status for ops monitoring.

GET /health
Returns HTTP 200 if all critical subsystems OK, 503 if any are degraded.

Response shape:
{
  "ok": bool,
  "ts": <unix>,
  "subsystems": {
    "state_store":  {"ok": bool, "detail": str},
    "vp_cache":     {"ok": bool, "detail": str},
    "last_ingest":  {"ok": bool, "detail": str, "age_s": int|null},
    "kill_switch":  {"ok": bool, "detail": str, "today_r": float|null},
    "pending_orders": {"ok": bool, "detail": str, "count": int}
  }
}

Critical subsystems (any fail → HTTP 503): state_store, last_ingest
Advisory subsystems (degrade → logged, still 200): vp_cache, kill_switch, pending_orders
"""

from __future__ import annotations

import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)

ROOT = Path(__file__).resolve().parent.parent.parent

_INGEST_STALE_S = 120    # last ingest older than this = degraded
_VP_STALE_S = 3600       # VP cache older than this = advisory warning


def _check_state_store(settings: dict) -> dict:
    try:
        from pipeline.state_store import store
        symbol = settings["instrument"]["symbol"]
        tf = settings["instrument"]["primary_tf"]
        bars = store().recent(symbol, tf, 1)
        if not bars:
            return {"ok": False, "detail": f"no bars for {symbol}/{tf}"}
        age = int(time.time()) - bars[-1].close_ts
        return {"ok": True, "detail": f"last bar {age}s ago ({symbol}/{tf})"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_vp_cache(settings: dict) -> dict:
    try:
        from pipeline.features.vp_cache import get as vp_get
        symbol = settings["instrument"]["symbol"]
        vp = vp_get(symbol, "daily")
        if not vp:
            return {"ok": False, "detail": f"no daily VP for {symbol}"}
        poc = vp.get("poc")
        ts = vp.get("built_ts") or vp.get("ts")
        age_s = int(time.time() - ts) if ts else None
        stale = age_s is not None and age_s > _VP_STALE_S
        detail = f"poc={poc} built {age_s}s ago" if age_s is not None else f"poc={poc}"
        return {"ok": not stale, "detail": detail, "age_s": age_s}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_last_ingest(settings: dict) -> dict:
    try:
        from pipeline.state_store import store
        symbol = settings["instrument"]["symbol"]
        tf = settings["instrument"]["primary_tf"]
        bars = store().recent(symbol, tf, 1)
        if not bars:
            return {"ok": False, "detail": "no bars in state_store", "age_s": None}
        age_s = int(time.time()) - bars[-1].close_ts
        ok = age_s < _INGEST_STALE_S
        return {
            "ok": ok,
            "detail": f"last bar {age_s}s ago" + ("" if ok else f" (stale >{_INGEST_STALE_S}s)"),
            "age_s": age_s,
        }
    except Exception as e:
        return {"ok": False, "detail": str(e), "age_s": None}


def _check_kill_switch(settings: dict) -> dict:
    try:
        import json
        import datetime
        positions_path = ROOT / "data" / "positions.jsonl"
        if not positions_path.exists():
            return {"ok": True, "detail": "no positions file", "today_r": None}

        today = datetime.date.today().isoformat()
        today_r = 0.0
        with positions_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                closed_at = p.get("closed_at") or ""
                if not closed_at.startswith(today):
                    continue
                today_r += float(p.get("realized_r") or 0.0)

        floor = float((settings.get("risk") or {}).get("daily", {}).get("max_dd_r") or 5.0)
        halted = today_r <= -floor
        return {
            "ok": not halted,
            "detail": f"today_R={today_r:.2f}" + (" — HALTED" if halted else f" (floor -{floor}R)"),
            "today_r": round(today_r, 4),
        }
    except Exception as e:
        return {"ok": True, "detail": f"check error: {e}", "today_r": None}


def _check_pending_orders() -> dict:
    try:
        path = ROOT / "data" / "pending_orders.jsonl"
        if not path.exists():
            return {"ok": True, "detail": "no pending_orders file", "count": 0}
        count = sum(1 for line in path.open() if line.strip())
        return {"ok": True, "detail": f"{count} pending orders on disk", "count": count}
    except Exception as e:
        return {"ok": True, "detail": f"check error: {e}", "count": None}


@bp.get("/health")
def health():
    settings: dict = current_app.config.get("FB_SETTINGS") or {}

    subsystems = {
        "state_store":    _check_state_store(settings),
        "vp_cache":       _check_vp_cache(settings),
        "last_ingest":    _check_last_ingest(settings),
        "kill_switch":    _check_kill_switch(settings),
        "pending_orders": _check_pending_orders(),
    }

    # Critical: state_store + last_ingest must be OK for 200
    critical_ok = subsystems["state_store"]["ok"] and subsystems["last_ingest"]["ok"]
    overall_ok = all(v["ok"] for v in subsystems.values())

    payload = {
        "ok": overall_ok,
        "ts": int(time.time()),
        "subsystems": subsystems,
    }
    status = 200 if critical_ok else 503
    return jsonify(payload), status
