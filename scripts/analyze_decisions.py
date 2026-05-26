"""Analyze all decisions + cycle outcomes → write HTML report.

Joins:
  data/decisions.jsonl     — Mode 1 (Claude) decisions
  data/mode_compare.jsonl  — Mode 2 (rules) decisions (dry-run logged)
  data/positions.jsonl     — cycle opens / closes / invalidates / events

Output:
  data/decisions_report.html — open in browser; sortable categorized table

Categories captured:
  - Symbol, mode (M1/M2), side, validator status
  - Outcome: tp_hit | sl_hit | invalidated | still_open | not_dispatched
  - Realized R, R:R intent, duration mins, close reason
  - Side-by-side M1 vs M2 agreement per bar
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M") if ts else "-"


def load_decisions() -> list[dict]:
    p = ROOT / "data" / "decisions.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_compare() -> list[dict]:
    p = ROOT / "data" / "mode_compare.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_positions() -> tuple[dict, list[dict]]:
    """Returns (positions_by_id, raw_events)."""
    p = ROOT / "data" / "positions.jsonl"
    if not p.exists():
        return {}, []
    raw = []
    for line in p.read_text().splitlines():
        try:
            raw.append(json.loads(line))
        except Exception:
            pass
    positions: dict = {}
    for e in raw:
        pid = e.get("position_id", "")
        t = e.get("type", "")
        if t == "open":
            positions[pid] = {
                "position_id": pid,
                "symbol": e.get("symbol"),
                "side": e.get("side"),
                "entry": e.get("entry"),
                "sl": e.get("stop_loss"),
                "tp": e.get("take_profit"),
                "bar_id": e.get("bar_id"),
                "open_ts": e.get("ts"),
                "status": "open",
                "close_ts": None,
                "close_reason": None,
                "realized_r": None,
                "leg_count": 1,
            }
        elif t == "add_leg" and pid in positions:
            positions[pid]["leg_count"] += 1
        elif t in ("close", "invalidate") and pid in positions:
            positions[pid]["status"] = t + "d" if t == "close" else "invalidated"
            positions[pid]["close_ts"] = e.get("ts")
            positions[pid]["close_reason"] = e.get("reason", "")
            positions[pid]["realized_r"] = e.get("realized_r")
    return positions, raw


def link_decisions_to_positions(decisions: list[dict], positions: dict) -> list[dict]:
    """Augment each Mode 1 decision with its cycle outcome (if dispatched)."""
    # Index positions by (symbol, bar_id)
    idx = {(p["symbol"], p["bar_id"]): p for p in positions.values()}
    enriched = []
    for d in decisions:
        sym = d.get("symbol", "")
        bar = d.get("bar_id", "")
        pos = idx.get((sym, bar))
        row = {
            "ts": d.get("ts", 0),
            "symbol": sym,
            "bar_id": bar,
            "mode": "M1",
            "side": d.get("decision", {}).get("side"),
            "conf": d.get("decision", {}).get("confidence", 0),
            "entry": d.get("decision", {}).get("entry"),
            "sl": d.get("decision", {}).get("stop_loss"),
            "tp": d.get("decision", {}).get("take_profit"),
            "validator": d.get("validator_reason"),
            "rationale": (d.get("decision", {}).get("rationale") or "")[:200],
        }
        if pos:
            row["dispatched"] = True
            row["status"] = pos["status"]
            row["close_reason"] = pos["close_reason"]
            row["realized_r"] = pos["realized_r"]
            row["leg_count"] = pos["leg_count"]
            row["duration_min"] = (pos["close_ts"] - pos["open_ts"]) / 60 if pos["close_ts"] else None
            row["position_id"] = pos["position_id"][:8]
        else:
            row["dispatched"] = False
            row["status"] = "not_dispatched"
            row["close_reason"] = None
            row["realized_r"] = None
            row["leg_count"] = 0
            row["duration_min"] = None
            row["position_id"] = ""
        enriched.append(row)
    return enriched


def link_compare(compare: list[dict]) -> list[dict]:
    out = []
    for c in compare:
        out.append({
            "ts": c.get("ts", 0),
            "symbol": c.get("symbol", ""),
            "bar_id": c.get("bar_id", ""),
            "mode": "M2",
            "side": c.get("side"),
            "bias_strength": c.get("bias_strength"),
            "score": c.get("score"),
            "votes": c.get("votes", []),
            "dry_run": c.get("dry_run", True),
            "status": "dry_run" if c.get("dry_run") else (
                "dispatched" if c.get("dispatched_position_id") else "skipped"),
        })
    return out


def compute_summary(m1: list[dict], m2: list[dict]) -> dict:
    s = {"m1": defaultdict(lambda: defaultdict(int)),
         "m2": defaultdict(lambda: defaultdict(int))}
    for r in m1:
        sym = r["symbol"]
        s["m1"][sym]["total"] += 1
        s["m1"][sym][f"side_{r['side']}"] += 1
        if r["validator"]:
            s["m1"][sym]["rejected"] += 1
        if r["dispatched"]:
            s["m1"][sym]["dispatched"] += 1
            if r["status"] == "closed":
                s["m1"][sym]["closed"] += 1
                if r["close_reason"] and "tp_hit" in str(r["close_reason"]):
                    s["m1"][sym]["tp_hit"] += 1
                elif r["close_reason"] and "sl_hit" in str(r["close_reason"]):
                    s["m1"][sym]["sl_hit"] += 1
            elif r["status"] == "invalidated":
                s["m1"][sym]["invalidated"] += 1
            elif r["status"] == "open":
                s["m1"][sym]["still_open"] += 1
            if r["realized_r"] is not None:
                s["m1"][sym]["sum_r"] += r["realized_r"]
    for r in m2:
        sym = r["symbol"]
        s["m2"][sym]["total"] += 1
        s["m2"][sym][f"side_{r['side']}"] += 1
    return s


def ab_alignment(m1: list[dict], m2: list[dict]) -> list[dict]:
    m1_idx = {(r["symbol"], r["bar_id"]): r for r in m1}
    m2_idx = {(r["symbol"], r["bar_id"]): r for r in m2}
    keys = sorted(set(m1_idx) | set(m2_idx),
                  key=lambda k: m1_idx.get(k, m2_idx.get(k, {})).get("ts", 0))
    out = []
    for k in keys:
        r1 = m1_idx.get(k)
        r2 = m2_idx.get(k)
        sym, bar = k
        ts = (r1 or r2).get("ts", 0)
        out.append({
            "ts": ts, "symbol": sym, "bar_id": bar,
            "m1_side": r1["side"] if r1 else None,
            "m1_conf": r1["conf"] if r1 else None,
            "m1_validator": r1["validator"] if r1 else None,
            "m1_outcome": r1["status"] if r1 else None,
            "m1_r": r1["realized_r"] if r1 else None,
            "m2_side": r2["side"] if r2 else None,
            "m2_score": r2["score"] if r2 else None,
            "m2_bias": r2["bias_strength"] if r2 else None,
            "agree": (r1["side"] == r2["side"]) if (r1 and r2) else None,
        })
    return out


def render_html(m1: list[dict], m2: list[dict], summary: dict, ab: list[dict]) -> str:
    css = """
<style>
body { font-family: -apple-system, sans-serif; padding: 20px; max-width: 1600px; margin: 0 auto; background: #fafafa; }
h1 { margin: 0 0 8px; }
h2 { margin: 28px 0 8px; border-bottom: 2px solid #333; padding-bottom: 4px; }
.muted { color: #777; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
th { background: #2c3e50; color: white; padding: 8px 10px; text-align: left; font-weight: 600; cursor: pointer; user-select: none; position: sticky; top: 0; }
th:hover { background: #34495e; }
td { padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
tr:hover td { background: #f8f9fb; }
.long { color: #27ae60; font-weight: bold; }
.short { color: #c0392b; font-weight: bold; }
.flat { color: #888; }
.r-pos { color: #27ae60; font-weight: bold; }
.r-neg { color: #c0392b; font-weight: bold; }
.rejected { color: #c0392b; font-style: italic; font-size: 11px; }
.agree-y { color: #27ae60; font-weight: bold; }
.agree-n { color: #c0392b; font-weight: bold; }
.cards { display: flex; gap: 16px; flex-wrap: wrap; }
.card { background: white; padding: 14px 18px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); min-width: 200px; }
.card h3 { margin: 0 0 8px; font-size: 14px; color: #555; }
.card .num { font-size: 24px; font-weight: bold; color: #2c3e50; }
.filter-bar { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
.filter-bar input, .filter-bar select { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; }
.short-text { max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.short-text:hover { white-space: normal; overflow: visible; }
.votes { font-size: 11px; color: #555; }
</style>
"""
    js = """
<script>
function filterTable(tableId, inputId) {
    const input = document.getElementById(inputId);
    const filter = input.value.toUpperCase();
    const table = document.getElementById(tableId);
    const rows = table.getElementsByTagName('tr');
    for (let i = 1; i < rows.length; i++) {
        const cells = rows[i].getElementsByTagName('td');
        let text = '';
        for (const c of cells) text += c.textContent + ' ';
        rows[i].style.display = text.toUpperCase().includes(filter) ? '' : 'none';
    }
}
function sortTable(tableId, colIdx) {
    const table = document.getElementById(tableId);
    const rows = Array.from(table.getElementsByTagName('tr')).slice(1);
    const asc = table.dataset.sortCol == colIdx ? !(table.dataset.sortAsc === 'true') : true;
    rows.sort((a, b) => {
        const av = a.getElementsByTagName('td')[colIdx].textContent.trim();
        const bv = b.getElementsByTagName('td')[colIdx].textContent.trim();
        const an = parseFloat(av); const bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    const tbody = table.tBodies[0];
    rows.forEach(r => tbody.appendChild(r));
    table.dataset.sortCol = colIdx;
    table.dataset.sortAsc = asc;
}
</script>
"""

    def fmt_side(s):
        if s in ("long", "short"):
            return f'<span class="{s}">{s.upper()}</span>'
        return f'<span class="flat">{s or "-"}</span>'

    def fmt_r(r):
        if r is None: return "-"
        cls = "r-pos" if r > 0 else ("r-neg" if r < 0 else "")
        return f'<span class="{cls}">{r:+.2f}</span>'

    parts = [f"<!doctype html><html><head><meta charset=utf-8><title>FootprintBiot Decisions Report</title>{css}{js}</head><body>"]
    parts.append(f"<h1>FootprintBiot Decisions Report</h1>")
    parts.append(f"<div class='muted'>Generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')} — "
                 f"{len(m1)} Mode 1 decisions, {len(m2)} Mode 2 decisions, {len(ab)} aligned bars</div>")

    # ── Summary cards ──────────────────────────────────────────────────────
    parts.append("<h2>Summary by Symbol</h2>")
    parts.append("<div class='cards'>")
    for sym, st in sorted(summary["m1"].items()):
        sum_r = st.get("sum_r", 0)
        tp = st.get("tp_hit", 0); sl = st.get("sl_hit", 0)
        inv = st.get("invalidated", 0); op = st.get("still_open", 0)
        rej = st.get("rejected", 0)
        wr = (tp / (tp + sl) * 100) if (tp + sl) > 0 else 0
        parts.append(f"""<div class='card'>
<h3>{sym} (Mode 1)</h3>
<div class='num'>{st.get('total',0)}</div>
<div class='muted'>decisions ({rej} rejected)</div>
<div style='margin-top:8px;font-size:12px;'>
  long={st.get('side_long',0)} short={st.get('side_short',0)} flat={st.get('side_flat',0)}<br>
  TP={tp} SL={sl} invalidated={inv} open={op}<br>
  WR={wr:.0f}% | sum_R={sum_r:+.2f}
</div>
</div>""")
    for sym, st in sorted(summary["m2"].items()):
        parts.append(f"""<div class='card'>
<h3>{sym} (Mode 2)</h3>
<div class='num'>{st.get('total',0)}</div>
<div class='muted'>decisions (dry-run)</div>
<div style='margin-top:8px;font-size:12px;'>
  long={st.get('side_long',0)} short={st.get('side_short',0)} flat={st.get('side_flat',0)}
</div>
</div>""")
    parts.append("</div>")

    # ── A/B Alignment ──────────────────────────────────────────────────────
    if ab:
        parts.append("<h2>Mode 1 vs Mode 2 Alignment</h2>")
        parts.append("<div class='filter-bar'><input id='ab_filter' onkeyup=\"filterTable('ab_table','ab_filter')\" placeholder='Filter...' style='flex:1'></div>")
        parts.append("<table id='ab_table'><thead><tr>")
        for i, h in enumerate(["Time IST", "Symbol", "M1 side", "M1 conf", "M1 outcome", "M1 R", "M2 side", "M2 score", "M2 bias", "Agree"]):
            parts.append(f"<th onclick=\"sortTable('ab_table',{i})\">{h}</th>")
        parts.append("</tr></thead><tbody>")
        for r in reversed(ab):
            agree = r["agree"]
            ag = "-"
            if agree is True: ag = '<span class="agree-y">✓</span>'
            elif agree is False: ag = '<span class="agree-n">✗</span>'
            outcome = (r["m1_outcome"] or "-").replace("not_dispatched", "rejected")
            parts.append(f"""<tr>
<td>{_ist(r['ts'])}</td>
<td>{r['symbol']}</td>
<td>{fmt_side(r['m1_side'])}</td>
<td>{(f"{r['m1_conf']:.2f}" if r['m1_conf'] is not None else '-')}</td>
<td>{outcome}</td>
<td>{fmt_r(r['m1_r'])}</td>
<td>{fmt_side(r['m2_side'])}</td>
<td>{r['m2_score'] if r['m2_score'] is not None else '-'}</td>
<td>{r['m2_bias'] or '-'}</td>
<td>{ag}</td>
</tr>""")
        parts.append("</tbody></table>")

    # ── Detail: Mode 1 decisions with outcomes ────────────────────────────
    parts.append("<h2>Mode 1 Decisions Detail</h2>")
    parts.append("<div class='filter-bar'><input id='m1_filter' onkeyup=\"filterTable('m1_table','m1_filter')\" placeholder='Filter (e.g. BTCUSDT short tp_hit)...' style='flex:1'></div>")
    parts.append("<table id='m1_table'><thead><tr>")
    for i, h in enumerate(["Time IST", "Symbol", "Side", "Conf", "Entry", "SL", "TP", "Validator", "Outcome", "R", "Legs", "Dur(m)", "Pos", "Rationale"]):
        parts.append(f"<th onclick=\"sortTable('m1_table',{i})\">{h}</th>")
    parts.append("</tr></thead><tbody>")
    for r in reversed(m1):
        validator = r["validator"] or ""
        val_cell = f'<span class="rejected">{validator[:60]}</span>' if validator else "ok"
        outcome = r["status"]
        if outcome == "closed":
            outcome = (r["close_reason"] or "closed").split("@")[0].strip()
        elif outcome == "invalidated":
            outcome = "invalidated"
        elif outcome == "not_dispatched":
            outcome = "rejected" if validator else "flat/no-dispatch"
        dur = f"{r['duration_min']:.0f}" if r["duration_min"] else "-"
        parts.append(f"""<tr>
<td>{_ist(r['ts'])}</td>
<td>{r['symbol']}</td>
<td>{fmt_side(r['side'])}</td>
<td>{r['conf']:.2f}</td>
<td>{r['entry'] or '-'}</td>
<td>{r['sl'] or '-'}</td>
<td>{r['tp'] or '-'}</td>
<td>{val_cell}</td>
<td>{outcome}</td>
<td>{fmt_r(r['realized_r'])}</td>
<td>{r['leg_count']}</td>
<td>{dur}</td>
<td>{r['position_id']}</td>
<td class='short-text' title="{(r['rationale'] or '').replace(chr(34),'&quot;')}">{(r['rationale'] or '')[:120]}</td>
</tr>""")
    parts.append("</tbody></table>")

    # ── Detail: Mode 2 decisions ───────────────────────────────────────────
    if m2:
        parts.append("<h2>Mode 2 Decisions Detail</h2>")
        parts.append("<div class='filter-bar'><input id='m2_filter' onkeyup=\"filterTable('m2_table','m2_filter')\" placeholder='Filter...' style='flex:1'></div>")
        parts.append("<table id='m2_table'><thead><tr>")
        for i, h in enumerate(["Time IST", "Symbol", "Side", "Score", "Bias", "Votes"]):
            parts.append(f"<th onclick=\"sortTable('m2_table',{i})\">{h}</th>")
        parts.append("</tr></thead><tbody>")
        for r in reversed(m2):
            votes_str = " | ".join(f"{v['m']}({v['d']:+.1f}, w={v['w']:.1f})" for v in (r.get("votes") or []))
            parts.append(f"""<tr>
<td>{_ist(r['ts'])}</td>
<td>{r['symbol']}</td>
<td>{fmt_side(r['side'])}</td>
<td>{r['score'] if r['score'] is not None else '-'}</td>
<td>{r['bias_strength'] or '-'}</td>
<td class='votes'>{votes_str}</td>
</tr>""")
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "".join(parts)


def main():
    decisions = load_decisions()
    compare_raw = load_compare()
    positions, _ = load_positions()

    m1 = link_decisions_to_positions(decisions, positions)
    m2 = link_compare(compare_raw)
    summary = compute_summary(m1, m2)
    ab = ab_alignment(m1, m2)

    html = render_html(m1, m2, summary, ab)
    out = ROOT / "data" / "decisions_report.html"
    out.write_text(html)
    print(f"Wrote {out}")
    print(f"  Mode 1: {len(m1)} decisions  Mode 2: {len(m2)} decisions")
    print(f"  Aligned bars: {len(ab)}")
    print(f"\n  Open with: open {out}")


if __name__ == "__main__":
    main()
