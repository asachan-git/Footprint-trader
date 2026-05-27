"""Analyze trading decisions made since the LAST Flask restart.

Joins:
  data/decisions.jsonl    — Mode 1 decisions
  data/mode_compare.jsonl — Mode 2 decisions
  data/positions.jsonl    — cycle events (open/add_leg/close/invalidate)

For each cycle, replays actual candle data after open to verify if
TP/SL was hit, max favorable / max adverse excursion, and the path
between entry and exit.

Output:
  data/since_restart_report.html
  + console summary
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M") if ts else "-"


def last_restart_epoch() -> int:
    """Parse logs/flask.log for the most recent 'Serving Flask' line, return its IST timestamp."""
    log = ROOT / "logs" / "flask.log"
    if not log.exists():
        return 0
    last_ts = 0
    # Look for 'INFO [startup]' lines — they appear right before 'Serving Flask'
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) IST INFO \[startup\]")
    with log.open() as fh:
        for line in fh:
            m = pat.match(line)
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                last_ts = int(dt.timestamp())
    return last_ts


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_positions() -> dict:
    raw = load_jsonl(ROOT / "data" / "positions.jsonl")
    pos: dict = {}
    for e in raw:
        pid = e.get("position_id", "")
        t = e.get("type", "")
        if t == "open":
            pos[pid] = {
                "position_id": pid, "symbol": e.get("symbol"), "side": e.get("side"),
                "entry": e.get("entry"), "sl": e.get("stop_loss"), "tp": e.get("take_profit"),
                "bar_id": e.get("bar_id"), "open_ts": e.get("ts"),
                "status": "open", "close_ts": None, "close_reason": None,
                "realized_r": None, "legs": [{"entry": e.get("entry"), "ts": e.get("ts")}],
            }
        elif t == "add_leg" and pid in pos:
            pos[pid]["legs"].append({"entry": e.get("entry"), "ts": e.get("ts")})
        elif t in ("close", "invalidate") and pid in pos:
            pos[pid]["status"] = "closed" if t == "close" else "invalidated"
            pos[pid]["close_ts"] = e.get("ts")
            pos[pid]["close_reason"] = e.get("reason", "")
            pos[pid]["realized_r"] = e.get("realized_r")
        elif t == "tp_adjust" and pid in pos:
            pos[pid]["tp"] = e.get("new_tp")
    return pos


_BAR_CACHE: dict = {}


def _load_persisted_bars(symbol: str, tf: str) -> list[dict]:
    """Read bars from data/footprint/{symbol}_{tf}.jsonl persistence."""
    key = (symbol, tf)
    if key in _BAR_CACHE:
        return _BAR_CACHE[key]
    p = ROOT / "data" / "footprint" / f"{symbol}_{tf}.jsonl"
    if not p.exists():
        _BAR_CACHE[key] = []
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    _BAR_CACHE[key] = out
    return out


def candle_replay_outcome(symbol: str, tf: str, open_ts: int, close_ts: int | None,
                          entry: float, sl: float, tp: float, side: str) -> dict:
    """Walk persisted bars between open and close. Compute MFE/MAE, TP/SL touched."""
    bars = _load_persisted_bars(symbol, tf)
    if not bars:
        return {}
    end = close_ts or int(datetime.now(IST).timestamp()) + 60
    in_window = [b for b in bars if open_ts <= b["close_ts"] <= end]
    if not in_window:
        return {"bars_in_window": 0}
    max_high = max(b["ohlc"]["h"] for b in in_window)
    min_low = min(b["ohlc"]["l"] for b in in_window)
    final_close = in_window[-1]["ohlc"]["c"]
    if side == "long":
        mfe = max_high - entry
        mae = entry - min_low
        tp_touched = max_high >= tp if tp else False
        sl_touched = min_low <= sl if sl else False
        unrealized = final_close - entry
    else:
        mfe = entry - min_low
        mae = max_high - entry
        tp_touched = min_low <= tp if tp else False
        sl_touched = max_high >= sl if sl else False
        unrealized = entry - final_close
    risk = abs(entry - sl) or 1e-9
    return {
        "bars_in_window": len(in_window),
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "final_close": round(final_close, 4),
        "mfe_pts": round(mfe, 4),
        "mae_pts": round(mae, 4),
        "mfe_r": round(mfe / risk, 2),
        "mae_r": round(-mae / risk, 2),
        "tp_touched": tp_touched,
        "sl_touched": sl_touched,
        "unrealized_pts": round(unrealized, 4),
        "unrealized_r": round(unrealized / risk, 2),
    }


def previous_restart_epoch() -> int:
    """Second-most-recent server start (the session BEFORE the current one)."""
    log = ROOT / "logs" / "flask.log"
    if not log.exists():
        return 0
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) IST INFO \[startup\]")
    starts = []
    with log.open() as fh:
        for line in fh:
            m = pat.match(line)
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                starts.append(int(dt.timestamp()))
    return starts[-2] if len(starts) >= 2 else 0


def main():
    import sys
    import time
    hours_arg = next((float(sys.argv[i + 1]) for i, a in enumerate(sys.argv) if a == "--hours" and i + 1 < len(sys.argv)), None)
    since_arg = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--since" and i + 1 < len(sys.argv)), None)

    if since_arg:
        since = int(since_arg)
        print(f"Filter: --since {since} ({_ist(since)})")
    elif hours_arg:
        since = int(time.time() - hours_arg * 3600)
        print(f"Filter: last {hours_arg}h (since {_ist(since)})")
    else:
        since = last_restart_epoch()
        if since == 0:
            since = int(time.time() - 4 * 3600)
            print(f"No restart marker found — falling back to last 4h (since {_ist(since)})")
        else:
            print(f"Filter: since last restart {_ist(since)}")

    all_decision_recs = load_jsonl(ROOT / "data" / "decisions.jsonl")
    # Build dispatch_result lookup: decision_id → dispatch_result dict
    dispatch_lookup: dict = {}
    for rec in all_decision_recs:
        if "dispatch_result" in rec and "decision_id" in rec:
            dispatch_lookup[rec["decision_id"]] = rec["dispatch_result"]
        elif "dispatched" in rec and "decision_id" in rec:
            dispatch_lookup[rec["decision_id"]] = rec["dispatched"]

    m1_raw = [d for d in all_decision_recs if d.get("ts", 0) >= since and "decision" in d]
    # Dedup per (symbol, bar_id) — keep highest-confidence decision per bar
    by_bar: dict = {}
    for d in m1_raw:
        k = (d.get("symbol"), d.get("bar_id"))
        cur = by_bar.get(k)
        if cur is None or (d.get("decision", {}).get("confidence", 0) > cur.get("decision", {}).get("confidence", 0)):
            by_bar[k] = d
    m1 = sorted(by_bar.values(), key=lambda d: d.get("ts", 0))
    m2 = [d for d in load_jsonl(ROOT / "data" / "mode_compare.jsonl") if d.get("ts", 0) >= since]
    positions = load_positions()
    # only cycles opened since restart
    cycles_since = {pid: p for pid, p in positions.items() if (p.get("open_ts") or 0) >= since}

    print(f"\nDecisions since restart: M1={len(m1)}, M2={len(m2)}, Cycles opened: {len(cycles_since)}")

    rows = []
    for d in m1:
        sym = d.get("symbol")
        bar = d.get("bar_id")
        dec = d.get("decision", {})
        # Find position opened on same bar_id + symbol
        matching = [p for p in cycles_since.values() if p["symbol"] == sym and p["bar_id"] == bar]
        pos = matching[0] if matching else None

        replay = {}
        if pos and pos["side"] in ("long", "short"):
            replay = candle_replay_outcome(
                symbol=sym, tf=d.get("tf", "15m"),
                open_ts=pos["open_ts"], close_ts=pos["close_ts"],
                entry=pos["entry"], sl=pos["sl"], tp=pos["tp"], side=pos["side"],
            )

        did = d.get("decision_id", "")
        dr = dispatch_lookup.get(did) or {}
        rows.append({
            "ts": d.get("ts"),
            "symbol": sym, "bar_id": bar,
            "side": dec.get("side"), "conf": dec.get("confidence", 0),
            "entry": dec.get("entry"), "sl": dec.get("stop_loss"), "tp": dec.get("take_profit"),
            "validator": d.get("validator_reason"),
            "dispatch_skipped": dr.get("skipped") if isinstance(dr, dict) else None,
            "dispatch_mode": dr.get("mode") if isinstance(dr, dict) else None,
            "rationale": (dec.get("rationale") or "")[:500],
            "entry_reasoning": dec.get("entry_reasoning", ""),
            "sl_reasoning": dec.get("sl_reasoning", ""),
            "target_reasoning": dec.get("target_reasoning", ""),
            "invalidation_note": dec.get("invalidation_note", ""),
            "position": pos,
            "replay": replay,
        })

    # ── Console summary ────────────────────────────────────────────────────
    print(f"\n{'Time':<17} {'Sym':<10} {'Side':<6} {'Conf':<5} {'Outcome':<22} {'R':<7} {'MFE/MAE R':<14}")
    print("-" * 100)
    for r in rows:
        outcome = "-"
        rr = "-"
        mfe_mae = "-"
        if r["position"]:
            p = r["position"]
            outcome = p["status"]
            if p["close_reason"]:
                outcome += ": " + str(p["close_reason"])[:25]
            if p["realized_r"] is not None:
                rr = f"{p['realized_r']:+.2f}"
            if r["replay"]:
                mfe = r["replay"].get("mfe_r", 0); mae = r["replay"].get("mae_r", 0)
                mfe_mae = f"{mfe:+.2f}/{mae:+.2f}"
        elif r["validator"]:
            outcome = "rejected: " + r["validator"][:20]
        elif r.get("dispatch_skipped"):
            outcome = "skipped: " + str(r["dispatch_skipped"])[:20]
        else:
            outcome = r["side"] if r["side"] == "flat" else "no-dispatch"
        print(f"{_ist(r['ts']):<17} {r['symbol']:<10} {r['side']:<6} {r['conf']:<5.2f} {outcome:<22} {rr:<7} {mfe_mae:<14}")

    # ── HTML output ────────────────────────────────────────────────────────
    write_html(rows, since)
    print(f"\nWrote {ROOT / 'data' / 'since_restart_report.html'}")
    print(f"Open with: open {ROOT / 'data' / 'since_restart_report.html'}")


def write_html(rows: list[dict], since: int):
    parts = ["""<!doctype html><html><head><meta charset=utf-8><title>Since-Restart Trade Analysis</title>
<style>
body { font-family: -apple-system, sans-serif; padding: 20px; max-width: 1700px; margin: 0 auto; background: #fafafa; }
h1 { margin: 0 0 8px; }
h2 { margin: 24px 0 12px; border-bottom: 2px solid #333; padding-bottom: 4px; }
.muted { color: #777; font-size: 13px; }
.case { background: white; padding: 16px 20px; margin: 16px 0; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #888; }
.case.tp { border-left-color: #27ae60; }
.case.sl { border-left-color: #c0392b; }
.case.invalidated { border-left-color: #f39c12; }
.case.open { border-left-color: #3498db; }
.case.rejected { border-left-color: #95a5a6; opacity: 0.7; }
.case.flat { border-left-color: #d0d0d0; opacity: 0.6; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.header .sym { font-weight: bold; font-size: 16px; }
.header .time { color: #777; font-size: 13px; }
.long { color: #27ae60; font-weight: bold; }
.short { color: #c0392b; font-weight: bold; }
.flat { color: #888; }
.r-pos { color: #27ae60; font-weight: bold; }
.r-neg { color: #c0392b; font-weight: bold; }
.field { display: inline-block; margin-right: 18px; font-size: 13px; }
.field .lbl { color: #888; }
.rationale { font-size: 13px; margin-top: 8px; color: #444; line-height: 1.5; }
.box { background: #f4f4f6; padding: 10px 12px; border-radius: 4px; margin: 8px 0; font-size: 12px; line-height: 1.6; }
.box .lbl { font-weight: bold; color: #555; }
.replay { background: #fff8e1; padding: 8px 12px; border-radius: 4px; margin-top: 8px; font-size: 12px; }
.replay .lbl { font-weight: bold; color: #b27d00; margin-right: 6px; }
</style></head><body>"""]
    parts.append(f"<h1>Trade Analysis Since Last Restart</h1>")
    parts.append(f"<div class='muted'>Showing decisions and cycles from <b>{_ist(since)}</b> onward. {len(rows)} decisions analyzed.</div>")

    # Summary stats
    closed = [r for r in rows if r["position"] and r["position"]["status"] == "closed"]
    invalidated = [r for r in rows if r["position"] and r["position"]["status"] == "invalidated"]
    open_pos = [r for r in rows if r["position"] and r["position"]["status"] == "open"]
    rejected = [r for r in rows if not r["position"] and r["validator"]]
    flat = [r for r in rows if r["side"] == "flat"]
    sum_r = sum((r["position"]["realized_r"] or 0) for r in rows if r["position"] and r["position"]["realized_r"])
    parts.append(f"""
<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0'>
  <div class='box'><span class='lbl'>Closed (TP/SL):</span> {len(closed)} | sum_R = <span class='{ "r-pos" if sum_r > 0 else "r-neg" }'>{sum_r:+.2f}</span></div>
  <div class='box'><span class='lbl'>Invalidated:</span> {len(invalidated)}</div>
  <div class='box'><span class='lbl'>Still open:</span> {len(open_pos)}</div>
  <div class='box'><span class='lbl'>Rejected (validator):</span> {len(rejected)}</div>
  <div class='box'><span class='lbl'>Flat (no setup):</span> {len(flat)}</div>
</div>
""")

    for r in rows:
        p = r["position"]
        replay = r["replay"]

        # CSS class for the card border
        css_class = "case"
        if p:
            if p["status"] == "closed" and p["close_reason"] and "tp_hit" in str(p["close_reason"]):
                css_class += " tp"
            elif p["status"] == "closed" and p["close_reason"] and "sl_hit" in str(p["close_reason"]):
                css_class += " sl"
            elif p["status"] == "invalidated":
                css_class += " invalidated"
            elif p["status"] == "open":
                css_class += " open"
        elif r["validator"]:
            css_class += " rejected"
        elif r["side"] == "flat":
            css_class += " flat"

        side_html = (f'<span class="{r["side"]}">{r["side"].upper()}</span>'
                     if r["side"] in ("long", "short") else f'<span class="flat">{r["side"] or "-"}</span>')

        parts.append(f'<div class="{css_class}">')
        parts.append(f"""<div class='header'>
<div><span class='sym'>{r['symbol']}</span> &nbsp; {side_html} &nbsp; <span class='muted'>conf={r['conf']:.2f}</span></div>
<div class='time'>{_ist(r['ts'])}</div>
</div>""")

        if r["entry"]:
            parts.append(f"""<div>
<span class='field'><span class='lbl'>entry:</span> {r['entry']}</span>
<span class='field'><span class='lbl'>SL:</span> {r['sl']}</span>
<span class='field'><span class='lbl'>TP:</span> {r['tp']}</span>
</div>""")

        if r["validator"]:
            parts.append(f"<div class='box'><span class='lbl'>REJECTED:</span> {r['validator']}</div>")

        # Cycle outcome
        if p:
            outcome = p["status"]
            close_info = ""
            if p["close_reason"]:
                close_info = f" — {p['close_reason']}"
            rr = ""
            if p["realized_r"] is not None:
                rr_class = "r-pos" if p["realized_r"] > 0 else "r-neg"
                rr = f" | realized_R = <span class='{rr_class}'>{p['realized_r']:+.2f}</span>"
            duration = ""
            if p["close_ts"] and p["open_ts"]:
                mins = (p["close_ts"] - p["open_ts"]) / 60
                duration = f" | duration {mins:.0f}m"
            parts.append(f"<div class='box'><span class='lbl'>CYCLE {p['position_id'][:8]}:</span> {outcome}{close_info}{rr}{duration} | legs={len(p['legs'])}</div>")

        # Candle replay
        if replay and replay.get("bars_in_window"):
            tp_mark = "✓" if replay.get("tp_touched") else "✗"
            sl_mark = "✓" if replay.get("sl_touched") else "✗"
            parts.append(f"""<div class='replay'>
<span class='lbl'>CANDLE REPLAY:</span>
{replay['bars_in_window']} bars | high={replay['max_high']} low={replay['min_low']} final={replay['final_close']} |
TP touched {tp_mark} | SL touched {sl_mark} |
MFE={replay['mfe_r']:+.2f}R MAE={replay['mae_r']:+.2f}R |
unrealized={replay['unrealized_r']:+.2f}R
</div>""")

        # Rationale + reasoning blocks
        if r["rationale"]:
            parts.append(f"<div class='rationale'><b>Rationale:</b> {r['rationale']}</div>")
        if r["entry_reasoning"]:
            parts.append(f"<div class='box'><span class='lbl'>Entry reasoning:</span><br>{r['entry_reasoning'].replace(chr(10), '<br>')}</div>")
        if r["sl_reasoning"]:
            parts.append(f"<div class='box'><span class='lbl'>SL reasoning:</span><br>{r['sl_reasoning'].replace(chr(10), '<br>')}</div>")
        if r["target_reasoning"]:
            parts.append(f"<div class='box'><span class='lbl'>Target reasoning:</span><br>{r['target_reasoning'].replace(chr(10), '<br>')}</div>")
        if r["invalidation_note"]:
            parts.append(f"<div class='box'><span class='lbl'>Invalidation note:</span><br>{r['invalidation_note']}</div>")

        parts.append("</div>")  # /case

    parts.append("</body></html>")
    out = ROOT / "data" / "since_restart_report.html"
    out.write_text("".join(parts))


if __name__ == "__main__":
    main()
