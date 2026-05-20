"""Daily bar journal generator.

Produces a human-readable Markdown file per instrument per trading day:
  data/journal/{symbol}/{YYYY-MM-DD}.md

Contains:
  1. Volume Profile summary (POC, VAH, VAL, shape, HVN/LVN, volume)
  2. Key observations (highs/lows, session delta, notable footprint events)
  3. Decisions made that day (from decisions.jsonl)
  4. 15m-resolution bar summary table with notable event annotations

Triggered from server/routes/ingest.py at each day boundary, and
available as a standalone backfill script.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JOURNAL_DIR = ROOT / "data" / "journal"
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%H:%M")


def _ist_full(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M IST")


def write_day_journal(
    symbol: str,
    primary_tf: str,
    date_key: str,
    session_anchor: object = 0,
) -> Path | None:
    """Generate journal for symbol on date_key (YYYY-MM-DD IST session label).

    session_anchor accepts the same forms as vp_cache: int UTC hour, tuple
    (tz, hour), or dict {tz, hour}. DST-aware for non-UTC anchors.

    Returns path to the generated file, or None if insufficient data.
    """
    from pipeline.state_store import store as _store
    import pipeline.features.vp_cache as _vpc

    out_dir = JOURNAL_DIR / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_key}.md"

    # Get session bounds for this date (DST-aware via vp_cache helper)
    from pipeline.features.vp_cache import _day_bounds as _db
    start_ts, end_ts = _db(date_key, session_anchor)
    # Derive the actual UTC hour for this specific date (may differ across DST)
    session_start_utc = datetime.fromtimestamp(start_ts, tz=timezone.utc).hour

    # Load bars for this session
    s = _store()
    all_bars = s.recent(symbol, primary_tf, 100_000)
    day_bars = [b for b in all_bars if start_ts <= b.close_ts < end_ts]

    if len(day_bars) < 10:
        return None

    # VP data
    vp = _load_vp_for_date(symbol, date_key)

    # Decisions for this day
    decisions = _load_decisions(symbol, start_ts, end_ts)

    # Big trade events
    big_trades = _load_big_trades(symbol, start_ts, end_ts)

    # Build 15m aggregated bars for the summary table
    bars_15m = _aggregate_15m(day_bars)

    # Notable events per 15m bucket
    events_map = _build_events_map(big_trades, decisions)

    # Compute session stats
    session_high = max(b.ohlc.h for b in day_bars)
    session_low  = min(b.ohlc.l for b in day_bars)
    session_delta = sum(b.delta or 0.0 for b in day_bars)
    open_price = day_bars[0].ohlc.o
    close_price = day_bars[-1].ohlc.c
    sources = {}
    for b in day_bars:
        sources[b.source] = sources.get(b.source, 0) + 1

    # Write markdown
    gen_ts = datetime.now(tz=_IST).strftime("%Y-%m-%d %H:%M IST")
    lines: list[str] = []

    lines.append(f"# {symbol} — {date_key} (IST session)")
    lines.append(f"*Session UTC {session_start_utc:02d}:00 start | "
                 f"Bars: {len(day_bars)} | "
                 f"Sources: {', '.join(f'{v} {k}' for k,v in sources.items())} | "
                 f"Generated: {gen_ts}*")
    lines.append("")

    # ── VP Summary ──────────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Volume Profile")
    lines.append("")
    if vp:
        poc   = vp.get("poc")
        vah   = vp.get("vah")
        val_  = vp.get("val")
        shape = vp.get("shape", "—")
        hvns  = vp.get("hvn_zones", [])
        lvns  = vp.get("lvn_zones", [])
        naked = vp.get("naked_poc")
        total_vol = vp.get("total_volume", 0)
        p_range = vp.get("price_range", 0)

        shape_desc = {
            "D": "balanced (mean reversion)",
            "P": "distribution (short bias)",
            "b": "accumulation (long bias)",
            "B": "bimodal (two-zone, fast-move setup)",
            "double": "bimodal (two zones)",
            "elongated": "trending (directional)",
            "thin": "thin data",
        }.get(shape, shape)

        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| POC | **{poc}** |")
        lines.append(f"| VAH | {vah} |")
        lines.append(f"| VAL | {val_} |")
        lines.append(f"| Shape | {shape} — {shape_desc} |")
        lines.append(f"| HVN Zones | {_fmt_zones(hvns) or 'none'} |")
        lines.append(f"| LVN Zones | {_fmt_zones(lvns) or 'none'} |")
        lines.append(f"| Naked POC | {naked or '—'} |")
        lines.append(f"| Total Volume | {total_vol:,.0f} |")
        lines.append(f"| Price Range | {p_range:.0f} pts |")
        lines.append(f"| Bar Count | {len(day_bars)} |")
    else:
        lines.append("*No VP data available for this session.*")
    lines.append("")

    # ── Key Observations ────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Key Observations")
    lines.append("")

    session_h_ts = max(day_bars, key=lambda b: b.ohlc.h).close_ts
    session_l_ts = min(day_bars, key=lambda b: b.ohlc.l).close_ts

    lines.append(f"- Session high: **{session_high}** ({_ist(session_h_ts)} IST) | "
                 f"Session low: **{session_low}** ({_ist(session_l_ts)} IST)")
    lines.append(f"- Session open: {open_price} | Session close: {close_price} | "
                 f"Change: {close_price - open_price:+.2f}")
    delta_dir = "net buying ↑" if session_delta > 50 else "net selling ↓" if session_delta < -50 else "balanced ↔"
    lines.append(f"- Session delta: **{session_delta:+.1f}** ({delta_dir})")

    if vp and vp.get("poc"):
        poc_pos = "above POC" if close_price > vp["poc"] else ("below POC" if close_price < vp["poc"] else "at POC")
        lines.append(f"- Day closed **{poc_pos}** ({close_price:.2f} vs POC {vp['poc']:.2f})")

    # Notable events from big_trades
    absorbed = [e for e in big_trades if e.get("outcome") == "absorbed"]
    pushed   = [e for e in big_trades if e.get("outcome") == "pushed"]
    exhausted = [e for e in big_trades if e.get("outcome") == "exhausted"]
    if absorbed:
        prices = sorted(set(round(e["price"], 0) for e in absorbed))
        lines.append(f"- Absorption events at: {prices} (sellers/buyers defended these levels)")
    if exhausted:
        prices = sorted(set(round(e["price"], 0) for e in exhausted))
        lines.append(f"- Exhaustion (sweep reversals) at: {prices}")
    if pushed:
        prices = sorted(set(round(e["price"], 0) for e in pushed))
        lines.append(f"- Breakout acceptance at: {prices}")
    lines.append("")

    # ── Decisions ───────────────────────────────────────────────────────────
    # Show only non-flat decisions; mention flat count
    non_flat = [d for d in decisions if d.get("decision", {}).get("side") not in ("flat", None)]
    flat_count = len(decisions) - len(non_flat)
    if decisions:
        lines.append("---")
        lines.append("")
        lines.append(f"## Decisions ({len(non_flat)} trade signals, {flat_count} flat skipped)")
        lines.append("")
        lines.append("| Time (IST) | Side | Entry | SL | TP | Conf | R:R | Result |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for d in non_flat:
            dec = d.get("decision", {})
            side = dec.get("side", "—")
            e = dec.get("entry")
            sl = dec.get("stop_loss")
            tp = dec.get("take_profit")
            conf = dec.get("confidence", 0)
            rr = "—"
            if e and sl and tp:
                risk = abs(e - sl)
                if risk > 0:
                    rr = f"{abs(tp - e) / risk:.1f}"
            result = _decision_result(d.get("decision_id", ""), symbol)
            t = _ist(d["ts"])
            icon = "📈" if side == "long" else "📉" if side == "short" else "—"
            lines.append(f"| {t} | {icon} {side} | {e or '—'} | {sl or '—'} | {tp or '—'} | {conf:.2f} | {rr} | {result} |")
        lines.append("")

    # ── 15m Bar Summary ─────────────────────────────────────────────────────
    if bars_15m:
        lines.append("---")
        lines.append("")
        lines.append("## Bar Summary (15m resolution)")
        lines.append("")
        lines.append("| Time (IST) | Open | High | Low | Close | Δ (delta) | Events |")
        lines.append("|---|---|---|---|---|---|---|")
        for bar in bars_15m:
            t = _ist(bar["close_ts"])
            ev = events_map.get(_bucket_ts(bar["close_ts"]), [])
            ev_str = " · ".join(ev) if ev else ""
            delta_str = f"{bar['delta']:+.1f}" if bar["delta"] is not None else "—"
            lines.append(f"| {t} | {bar['o']:.2f} | {bar['h']:.2f} | "
                         f"{bar['l']:.2f} | {bar['c']:.2f} | {delta_str} | {ev_str} |")
        lines.append("")

    content = "\n".join(lines)
    out_path.write_text(content)
    return out_path


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_vp_for_date(symbol: str, date_key: str) -> dict | None:
    import pipeline.features.vp_cache as _vpc
    hist = _vpc.get_history(symbol, "daily", n=7)
    for entry in reversed(hist):
        if entry.get("period_key") == date_key:
            return entry
    return _vpc.get(symbol, "daily")


def _load_decisions(symbol: str, start_ts: int, end_ts: int) -> list[dict]:
    f = ROOT / "data" / "decisions.jsonl"
    if not f.exists():
        return []
    result = []
    for line in f.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if d.get("symbol") == symbol and start_ts <= d.get("ts", 0) < end_ts:
                result.append(d)
        except Exception:
            pass
    return result


def _load_big_trades(symbol: str, start_ts: int, end_ts: int) -> list[dict]:
    f = ROOT / "data" / "big_trades.jsonl"
    if not f.exists():
        return []
    result = []
    for line in f.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if e.get("symbol") == symbol and start_ts <= e.get("ts", 0) < end_ts:
                result.append(e)
        except Exception:
            pass
    return result


def _aggregate_15m(bars_1m) -> list[dict]:
    """Aggregate 1m bars into 15m buckets."""
    buckets: dict[int, list] = {}
    for b in bars_1m:
        bts = math.ceil(b.close_ts / 900) * 900
        buckets.setdefault(bts, []).append(b)
    result = []
    for bts in sorted(buckets):
        grp = buckets[bts]
        result.append({
            "close_ts": bts,
            "o":     grp[0].ohlc.o,
            "h":     max(b.ohlc.h for b in grp),
            "l":     min(b.ohlc.l for b in grp),
            "c":     grp[-1].ohlc.c,
            "delta": sum(b.delta or 0 for b in grp),
        })
    return result


def _bucket_ts(ts: int) -> int:
    return math.ceil(ts / 900) * 900


def _build_events_map(big_trades: list[dict], decisions: list[dict]) -> dict[int, list[str]]:
    events: dict[int, list[str]] = {}

    for bt in big_trades:
        bts = _bucket_ts(bt["ts"])
        out = bt.get("outcome", "pending")
        side = bt.get("aggressor", "?")
        price = bt.get("price", 0)
        if out != "pending":
            label = f"{out}({side}@{price:.0f})"
            events.setdefault(bts, []).append(label)

    for d in decisions:
        bts = _bucket_ts(d["ts"])
        dec = d.get("decision", {})
        side = dec.get("side", "—")
        if side != "flat":
            label = f"SIGNAL:{side.upper()}"
            events.setdefault(bts, []).append(label)

    return events


def _decision_result(decision_id: str, symbol: str) -> str:
    """Look up the realized R for this decision from positions.jsonl."""
    pos_file = ROOT / "data" / "positions.jsonl"
    if not pos_file.exists():
        return "—"
    # We can't directly link decision_id to position; use the position file
    # as a proxy — look for position events near the same time
    return "—"


def _fmt_zones(zones: list[dict]) -> str:
    if not zones:
        return ""
    parts = [f"{z['low']:.0f}–{z['high']:.0f}" for z in zones[:4]]
    return ", ".join(parts)
