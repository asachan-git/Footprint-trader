"""Startup preflight — guarantee the data points the strategies need are present
BEFORE the server starts serving (and before any order can be armed).

Two things are verified, per the operator's requirement:
  1. FOOTPRINT depth — at least `min_days` of bars per (symbol, tf), and the most
     recent bar is within that window (the last N days aren't empty).
  2. VOLUME PROFILE — vp_cache has at least `min_days` recent daily periods per
     symbol, the latest period is fresh, and it carries HVN zones (LVN may be
     legitimately empty on a thin day → warning, not failure).

A FAILURE blocks startup (the operator chose preflight+block): create_app() raises
SystemExit with a banner naming exactly what's missing and the remediation command.
Warnings are logged but never block.

Run standalone to check without starting the server:
    python -m pipeline.startup_check          # exits 0 (pass) / 1 (fail)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

_DAY = 86400.0

# expected bars per day per tf (24/7); used only for a low-density WARNING
_BARS_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96}


def _cfg(settings: dict) -> dict:
    sc = (settings.get("startup_check") or {}) if isinstance(settings, dict) else {}
    vp_syms = (settings.get("vp_cache") or {}).get("symbols") or [settings["instrument"]["symbol"]]
    return {
        "enabled": bool(sc.get("enabled", True)),
        "min_days": int(sc.get("min_days", 5)),
        "max_period_age_days": int(sc.get("max_period_age_days", 3)),
        "require_hvn": bool(sc.get("require_hvn", True)),
        "stale_warn_hours": float(sc.get("stale_warn_hours", 6.0)),
        "min_density_pct": float(sc.get("min_density_pct", 0.5)),
        "symbols": list(sc.get("symbols", vp_syms)),
        "tfs": list(sc.get("tfs", settings.get("instrument", {}).get("timeframes", ["1m", "5m", "15m"]))),
    }


def _check_footprint(symbol: str, tf: str, c: dict, now: float,
                     fails: list, warns: list) -> None:
    from pipeline.state_store import store
    bars = store().recent(symbol, tf, 200_000)
    if not bars:
        fails.append(f"footprint {symbol}/{tf}: NO bars stored")
        return
    ts = sorted(b.close_ts for b in bars if getattr(b, "close_ts", 0))
    if not ts:
        fails.append(f"footprint {symbol}/{tf}: bars have no timestamps")
        return
    oldest, newest = ts[0], ts[-1]
    span_d = (newest - oldest) / _DAY
    newest_age_h = (now - newest) / 3600.0
    # FAIL: less than min_days of history span, or the last min_days are empty
    if span_d < c["min_days"]:
        fails.append(f"footprint {symbol}/{tf}: only {span_d:.1f}d span (need ≥{c['min_days']}d)")
    if newest_age_h / 24.0 > c["min_days"]:
        fails.append(f"footprint {symbol}/{tf}: newest bar {newest_age_h/24:.1f}d old "
                     f"(last {c['min_days']}d empty)")
    # WARN: stale feed, or thin density over the window
    if newest_age_h > c["stale_warn_hours"]:
        warns.append(f"footprint {symbol}/{tf}: last bar {newest_age_h:.1f}h old (feed stale?)")
    window_start = now - c["min_days"] * _DAY
    in_win = sum(1 for t in ts if t >= window_start)
    expected = _BARS_PER_DAY.get(tf, 0) * c["min_days"]
    if expected and in_win < expected * c["min_density_pct"]:
        warns.append(f"footprint {symbol}/{tf}: {in_win} bars in last {c['min_days']}d "
                     f"(<{c['min_density_pct']*100:.0f}% of {expected} — gaps/market-closed)")


def _check_vp(symbol: str, c: dict, vp: dict, today: datetime,
              fails: list, warns: list) -> None:
    blk = vp.get(symbol)
    if not isinstance(blk, dict):
        fails.append(f"vp_cache {symbol}: missing")
        return
    daily = blk.get("daily") or {}
    keys = sorted(k for k in daily if daily.get(k))
    if len(keys) < c["min_days"]:
        fails.append(f"vp_cache {symbol}: only {len(keys)} daily periods (need ≥{c['min_days']})")
        return
    latest = keys[-1]
    try:
        age_d = (today - datetime.strptime(latest, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
    except Exception:
        age_d = 999
    if age_d > c["max_period_age_days"]:
        fails.append(f"vp_cache {symbol}: latest period {latest} is {age_d}d old "
                     f"(>{c['max_period_age_days']}d — VP stale)")
    last = daily[latest] or {}
    if c["require_hvn"] and not (last.get("hvn_zones") or []):
        fails.append(f"vp_cache {symbol}: latest period {latest} has NO HVN zones")
    if not (last.get("lvn_zones") or []):
        warns.append(f"vp_cache {symbol}: latest period {latest} has no LVN zones (thin day?)")



# Setups that have ever been measured on this branch. Anything else reaching the
# emitter is a typo or an experiment that was never meant to trade live.
_KNOWN_SETUPS = {"hvn_inside_touch", "hvn_edge", "squeeze", "hvn_displacement",
                 "vp_levels", "lvn_displacement", "va", "vp_level_touch"}


def _check_risk_config(settings: dict, fails: list[str], warns: list[str]) -> None:
    """The risk floor has to be ON, and sizing has to be sane, before the server
    accepts a single order.

    These are the controls the Jun22-26 tree did not have at all: every exit in
    monitor_cycle fires on profit, neutrality or a structural event, so with the
    floor off a cycle that goes against the ladder has no bounded outcome. Booting
    with disaster_sl_usd: 0 is not a configuration choice, it is the state the
    account was blown in.
    """
    g = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}

    if float(g.get("disaster_sl_usd", 0) or 0) <= 0:
        fails.append("risk: disaster_sl_usd is 0 — legs would go out with no broker stop, "
                     "which is the pre-2026-08 state (15.7% of positions had an SL)")
    if float(g.get("cycle_max_loss_usd", 0) or 0) <= 0:
        fails.append("risk: cycle_max_loss_usd is 0 — no exit in monitor_cycle fires on a "
                     "loss, so a losing cycle has no bounded outcome")

    base_lot = float(g.get("base_lot", 0) or 0)
    max_lot = float(g.get("max_base_lot_guard", 0.5) or 0.5)
    if base_lot <= 0:
        fails.append("risk: base_lot is 0 or unset")
    elif base_lot > max_lot:
        fails.append(f"risk: base_lot {base_lot} exceeds max_base_lot_guard {max_lot} — "
                     f"the ramp to 1.5 on 2026-07-14 preceded a -126,891 day")

    trail = float(g.get("bias_trail_activate_usd", 0) or 0)
    if base_lot > 0 and trail > 0:
        per_lot = trail / base_lot
        if per_lot > 800:
            warns.append(f"risk: bias_trail_activate {trail} is {per_lot:.0f} per unit of "
                         f"base_lot; the measured Jun22-26 profile ran ~500")

    frac = float(g.get("bias_book_frac", 0.5) or 0.5)
    if not (0 < frac <= 1.0):
        fails.append(f"risk: bias_book_frac {frac} is outside (0, 1] — the value 100 was a "
                     f"sentinel meaning 'close the entire side', which removes the runner")

    import os
    for tf in ("1M", "5M", "15M", "1H"):
        raw = os.environ.get(f"FB_SETUPS_{tf}")
        if not raw:
            continue
        unknown = sorted(set(raw.split()) - _KNOWN_SETUPS)
        if unknown:
            fails.append(f"setups: FB_SETUPS_{tf} contains unknown {unknown} — an env "
                         f"override is not version-controlled, so a typo trades silently")


def run_preflight(settings: dict, now: float | None = None) -> dict:
    """Verify footprint + VP coverage. Returns {ok, failures, warnings, lines}."""
    c = _cfg(settings)
    if not c["enabled"]:
        return {"ok": True, "failures": [], "warnings": [], "lines": ["preflight disabled"]}
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    fails: list[str] = []
    warns: list[str] = []
    _check_risk_config(settings, fails, warns)
    for sym in c["symbols"]:
        for tf in c["tfs"]:
            _check_footprint(sym, tf, c, now, fails, warns)

    vp = {}
    try:
        import json
        from pathlib import Path
        vpf = Path(__file__).resolve().parent.parent / "data" / "vp_cache.json"
        vp = json.loads(vpf.read_text()) if vpf.exists() else {}
        if not vp:
            fails.append("vp_cache: data/vp_cache.json missing or empty")
    except Exception as e:
        fails.append(f"vp_cache: load error {e}")
    if vp:
        for sym in c["symbols"]:
            _check_vp(sym, c, vp, today, fails, warns)

    return {"ok": not fails, "failures": fails, "warnings": warns,
            "lines": [f"symbols={c['symbols']} tfs={c['tfs']} min_days={c['min_days']}"]}


def assert_ready(settings: dict) -> None:
    """Run preflight; on failure print a banner and raise SystemExit(1)."""
    import logging
    LOG = logging.getLogger(__name__)
    rep = run_preflight(settings)
    for w in rep["warnings"]:
        LOG.warning(f"[preflight] ⚠️  {w}")
    if rep["ok"]:
        LOG.info(f"[preflight] ✅ data ready — {rep['lines'][0]} "
                 f"({len(rep['warnings'])} warning(s))")
        return
    banner = ["", "═" * 64, "❌ STARTUP PREFLIGHT FAILED — refusing to start.",
              "   Required data is missing/stale:"]
    banner += [f"     • {f}" for f in rep["failures"]]
    banner += ["",
               "   Remediation:",
               "     • Footprint gaps (XAU/real)  : python -m scripts.fetch_history --days 7",
               "     • BTC tick-accurate rebuild  : python scripts/rebuild_footprint_history.py",
               "     • VP only (bars already ok)  : restart (VP rebuilds on boot)",
               "   Override (NOT for live): set startup_check.enabled=false in settings.yaml",
               "═" * 64, ""]
    msg = "\n".join(banner)
    LOG.error(msg)
    raise SystemExit(1)


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from server.app import load_settings
    rep = run_preflight(load_settings())
    print("\n".join(rep["lines"]))
    for w in rep["warnings"]:
        print(f"⚠️  {w}")
    if rep["ok"]:
        print(f"✅ PREFLIGHT PASS ({len(rep['warnings'])} warning(s))")
        return 0
    print("❌ PREFLIGHT FAIL:")
    for f in rep["failures"]:
        print(f"   • {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
