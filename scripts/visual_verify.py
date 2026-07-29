#!/usr/bin/env python3
"""visual_verify.py — offline chart-overlay verification for structural setups.

Reads historical bars from pipeline.state_store, runs existing detectors
(FootprintBiot's own choch/day_type/ict_fvg, plus GoldAITradingBot's
ob_scanner/trendline_engine/liquidity_pools/fvg_scanner/sweep_pattern),
and renders one self-contained plotly HTML file for visual inspection.

READ-ONLY / OFFLINE: no server, no EA, no live account touched. Purely a
verification tool for deciding which setups are worth wiring in live.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/visual_verify.py \
      --symbol XAUTUSDT --tf 15m --bars 500 \
      --concepts fvg,liquidity,orderblock,sweep,trendline,choch,regime,ict_fvg \
      --out /tmp/verify.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

FOOTPRINTBIOT_ROOT = Path(__file__).resolve().parent.parent
GAB_PYTHON_ROOT = Path("/Users/aniteksachan/Strategies/GoldAITradingBot/python")

sys.path.insert(0, str(FOOTPRINTBIOT_ROOT))
sys.path.insert(0, str(GAB_PYTHON_ROOT))

import plotly.graph_objects as go  # noqa: E402

from pipeline.state_store import store  # noqa: E402
from pipeline.features.atr import atr as compute_atr  # noqa: E402


def to_gab_bar(b):
    """FootprintBiot Bar -> GoldAITradingBot's plain Bar(t,o,h,l,c,v)."""
    from modules.state import Bar as GABBar
    vol = sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)
    return GABBar(t=b.close_ts, o=b.ohlc.o, h=b.ohlc.h, l=b.ohlc.l, c=b.ohlc.c, v=vol)


def _ts(bars, i):
    import datetime
    return datetime.datetime.fromtimestamp(bars[i].ohlc and bars[i].close_ts or 0)


def build_chart(symbol: str, tf: str, n_bars: int, concepts: set[str]) -> go.Figure:
    bars = store().recent(symbol, tf, n_bars)
    bars = [b for b in bars if getattr(b, "close_ts", 0) and b.close_ts < 9_000_000_000]
    if len(bars) < 20:
        raise SystemExit(f"not enough bars for {symbol}/{tf}: got {len(bars)}")

    import datetime
    xs = [datetime.datetime.fromtimestamp(b.close_ts) for b in bars]
    opens  = [b.ohlc.o for b in bars]
    highs  = [b.ohlc.h for b in bars]
    lows   = [b.ohlc.l for b in bars]
    closes = [b.ohlc.c for b in bars]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=xs, open=opens, high=highs, low=lows, close=closes,
        name="price", legendgroup="price",
    ))

    atr_val = compute_atr(bars, period=14)
    gab_bars = [to_gab_bar(b) for b in bars]

    x_end = xs[-1] + (xs[-1] - xs[-2]) * 20  # project ~20 bars into the future for open-ended overlays

    # ---- FVG (GoldAITradingBot fvg_scanner) ----
    if "fvg" in concepts:
        from modules.fvg_scanner import scan_fvgs
        all_levels = scan_fvgs(gab_bars)
        # Mitigated FVGs are filled/no longer a valid gap — drop them entirely,
        # don't just dim them (user request: removed, not shown-but-faded).
        levels = [lv for lv in all_levels if not lv.mitigated]
        for lv in levels:
            bi = max(0, min(lv.bar_index, len(bars) - 1))
            x0 = xs[bi]
            color = "rgba(0,180,0,0.18)" if lv.kind == "bull_fvg_top" else "rgba(200,0,0,0.18)"
            line_color = "green" if lv.kind == "bull_fvg_top" else "red"
            fig.add_shape(
                type="rect", x0=x0, x1=x_end, y0=lv.low, y1=lv.high,
                fillcolor=color, line=dict(color=line_color, width=1),
                legendgroup="fvg", name="fvg",
            )
            fig.add_annotation(x=x0, y=lv.high, text="FVG",
                                showarrow=False, font=dict(size=9, color=line_color),
                                yanchor="bottom")
        # dummy trace for legend toggle
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="green"), name=f"FVG ({len(levels)})",
                                  legendgroup="fvg"))

    # ---- Liquidity pools (GoldAITradingBot liquidity_pools) ----
    if "liquidity" in concepts:
        from modules.liquidity_pools import detect_liquidity_pools
        pools = detect_liquidity_pools(gab_bars, atr_val)
        for p in pools:
            color = "cyan" if p.kind == "BSL" else "magenta"
            fig.add_shape(type="line", x0=xs[0], x1=x_end, y0=p.price, y1=p.price,
                          line=dict(color=color, width=1, dash="dash" if not p.swept else "dot"),
                          legendgroup="liquidity")
            fig.add_annotation(x=xs[-1], y=p.price, text=f"{p.kind}{'(swept)' if p.swept else ''}",
                                showarrow=False, font=dict(size=9, color=color), xanchor="left")
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="cyan"), name=f"Liquidity pools ({len(pools)})",
                                  legendgroup="liquidity"))

    # ---- Orderblocks (GoldAITradingBot ob_scanner) ----
    ob_list = []
    if "orderblock" in concepts:
        from modules.ob_scanner import scan_obs
        ob_lookback = min(len(gab_bars), 200)
        ob_offset = len(gab_bars) - ob_lookback  # scan_obs returns bar_index relative to its internal `recent` slice
        ob_list = scan_obs(gab_bars, atr_val, lookback=ob_lookback)
        for ob in ob_list:
            bi = max(0, min(ob.bar_index + ob_offset, len(bars) - 1))
            x0 = xs[bi]
            color = "rgba(0,150,255,0.20)" if ob.kind == "bull_ob" else "rgba(255,140,0,0.20)"
            line_color = "dodgerblue" if ob.kind == "bull_ob" else "darkorange"
            fig.add_shape(type="rect", x0=x0, x1=x_end, y0=ob.ob_low, y1=ob.ob_high,
                          fillcolor=color, line=dict(color=line_color, width=1),
                          legendgroup="orderblock")
            fig.add_annotation(x=x0, y=ob.ob_low, text="OB", showarrow=False,
                                font=dict(size=9, color=line_color), yanchor="top")
        # scan_obs already filters mitigated OBs out (returns only unmitigated) —
        # this shows currently-live zones only, not full history.
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="dodgerblue"), name=f"Orderblocks ({len(ob_list)})",
                                  legendgroup="orderblock"))

    # ---- Sweep pattern (GoldAITradingBot sweep_pattern) — rolling scan ----
    if "sweep" in concepts:
        from modules.fvg_scanner import scan_fvgs as _scan_fvgs
        from modules.liquidity_pools import detect_liquidity_pools as _detect_lp, lp_summary_for_claude
        from modules.sweep_pattern import detect as sweep_detect
        hits = []
        step = max(1, len(gab_bars) // 150)  # cap rolling-scan cost on long windows
        for i in range(20, len(gab_bars), step):
            window = gab_bars[:i]
            fvgs_i = _scan_fvgs(window)
            lp_i = lp_summary_for_claude(_detect_lp(window, atr_val), bid=window[-1].c)
            res = sweep_detect(window, fvgs_i, lp_i, bid=window[-1].c, atr=atr_val)
            if res.active:
                hits.append((i - 1, res))
        for bi, res in hits:
            bi = max(0, min(bi, len(bars) - 1))
            x0 = xs[bi]
            fig.add_shape(type="line", x0=xs[0], x1=x_end, y0=res.sweep_low, y1=res.sweep_low,
                          line=dict(color="yellow", width=1, dash="dot"), legendgroup="sweep")
            fig.add_annotation(x=x0, y=res.signal_bar_low, text="SWEEP", showarrow=True,
                                arrowhead=2, font=dict(size=9, color="yellow"), ay=30)
            if res.tp_price:
                fig.add_shape(type="line", x0=x0, x1=x_end, y0=res.tp_price, y1=res.tp_price,
                              line=dict(color="lime", width=1, dash="dashdot"), legendgroup="sweep")
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="yellow"), name=f"Sweep patterns ({len(hits)})",
                                  legendgroup="sweep"))

    # ---- 3rd trendline touch (GoldAITradingBot trendline_engine) ----
    # Filtered: fractal-confirmed touches>=2 AND far anchor targets the
    # window's EXTREME liquidity pool (resistance -> extreme BSL above swing
    # highs, support -> extreme SSL below swing lows) — a line actually
    # running toward the liquidity it's meant to sweep, not just any 2-touch
    # line. (Angle filter dropped, see note below.)
    if "trendline" in concepts:
        from modules.trendline_engine import TrendlineEngine
        from modules.liquidity_pools import detect_liquidity_pools as _detect_lp_tl

        eng = TrendlineEngine(tf.upper() if tf.upper() in ("1H", "15M", "5M") else "15M")
        for i in range(20, len(gab_bars) + 1):
            eng.update(gab_bars[:i], atr_val)

        pools = _detect_lp_tl(gab_bars, atr_val)
        bsl_prices = [p.price for p in pools if p.kind == "BSL"]
        ssl_prices = [p.price for p in pools if p.kind == "SSL"]
        extreme_bsl = max(bsl_prices) if bsl_prices else None
        extreme_ssl = min(ssl_prices) if ssl_prices else None
        # 3.0xATR (was 1.0x): real qualifying support lines on this dataset sit
        # 1.5-2.6xATR from the extreme SSL — 1.0x admitted zero candidates.
        target_tol = 3.0 * atr_val if atr_val > 0 else 0.0

        def _targets_extreme_liquidity(tl) -> bool:
            # Target side follows the line's own slope (its trend direction),
            # not its kind — a descending resistance (lower highs, downtrend
            # continuation) projects toward the downside extreme (SSL) same
            # as a descending support would, and a rising support (uptrend)
            # projects toward the upside extreme (BSL) same as rising
            # resistance. Kind only says which pivots formed it, not which
            # way it's headed. anchor2 is always the later/closer pivot.
            if tl.slope > 0:
                extreme = extreme_bsl
            elif tl.slope < 0:
                extreme = extreme_ssl
            else:
                return False
            if extreme is None:
                return False
            return abs(tl.anchor2.price - extreme) <= target_tol

        # Angle filter dropped (2026-07-27, user decision): a 30-60deg slope
        # under ANY ATR-normalized convention is close to unsatisfiable for a
        # real multi-touch trendline — real candidates in this dataset measured
        # 0.4-4deg, since a genuine multi-bar trendline can't also move >0.5
        # ATR/bar (what 30-60deg would require) without breaking after 1-2 bars.
        # Relying on touches>=2 (fractal-confirmed) + extreme-liquidity-target
        # instead — both are real, satisfiable filters on this data.
        candidates = [
            tl for tl in eng._active_tls
            if not tl.broken
            and tl.touches >= 2
            and _targets_extreme_liquidity(tl)
        ]

        # Per kind, keep only ONE line: "targets the extreme liquidity" is the
        # GATE (candidates already passed it above) — among those, pick the
        # FEWEST touches (>=2 floor) as the actual selector. Fewest-touches =
        # freshest / least-confirmed-yet extreme line, not the most extreme
        # anchor price (an old line re-touched 16x is stale, not "the one").
        kept_tls = []
        for kind in ("support", "resistance"):
            same_kind = [tl for tl in candidates if tl.kind == kind]
            if not same_kind:
                continue
            same_kind.sort(key=lambda tl: tl.touches)
            kept_tls.append(same_kind[0])

        kept_ids = {(tl.kind, tl.anchor1.bar_index) for tl in kept_tls}
        actions = [a for a in eng.to_chart_actions()
                   if (a["kind"], int(a["id"].rsplit("_", 1)[-1])) in kept_ids]

        for act in actions:
            import datetime as _dt
            x0 = _dt.datetime.fromtimestamp(act["t1"])
            x1 = _dt.datetime.fromtimestamp(act["t2"])
            color = "lime" if act["signal"] == "fakeout" else ("orange" if act["signal"] == "continuation" else "gray")
            fig.add_shape(type="line", x0=x0, x1=x1, y0=act["p1"], y1=act["p2"],
                          line=dict(color=color, width=2 if act["signal"] else 1),
                          legendgroup="trendline")
            label = f"{act['kind']} touches={act['touches']} strength={act['strength']}"
            if act["signal"]:
                label += f" [{act['signal'].upper()}]"
            fig.add_annotation(x=x1, y=act["p2"], text=label, showarrow=False,
                                font=dict(size=9, color=color), xanchor="left")
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="lime"),
                                  name=f"Trendlines ({len(actions)}, filtered: touches>=2 + extreme-liquidity target)",
                                  legendgroup="trendline"))

    # ---- CVD divergence (FootprintBiot's own cvd_candlestick) ----
    # Marks only at confirmed swing pivots (price vs CVD regular divergence),
    # not every bar — computes CVD itself from native bars, no adapter needed.
    if "cvd_div" in concepts:
        from pipeline.features.cvd_candlestick import scan_divergences
        divs = scan_divergences(bars)
        ts_to_idx = {b.close_ts: i for i, b in enumerate(bars)}
        for d in divs:
            bi = ts_to_idx.get(d["ts"])
            if bi is None:
                continue
            color = "red" if d["type"] == "bear" else "lime"
            symbol_ = "triangle-down" if d["type"] == "bear" else "triangle-up"
            y = highs[bi] if d["type"] == "bear" else lows[bi]
            fig.add_trace(go.Scatter(
                x=[xs[bi]], y=[y], mode="markers",
                marker=dict(symbol=symbol_, size=12, color=color,
                            opacity=0.5 if d.get("live") else 1.0),
                legendgroup="cvd_div", showlegend=False,
            ))
            fig.add_annotation(
                x=xs[bi], y=y, text=f"CVDdiv{'*' if d.get('live') else ''}",
                showarrow=False, font=dict(size=9, color=color),
                yanchor="top" if d["type"] == "bear" else "bottom",
            )
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                  marker=dict(color="lime", symbol="triangle-up"),
                                  name=f"CVD divergences ({len(divs)})",
                                  legendgroup="cvd_div"))

    fig.update_layout(
        title=f"{symbol} {tf} — {n_bars} bars — concepts: {','.join(sorted(concepts)) or 'none'}",
        xaxis_rangeslider_visible=False,
        height=800,
        template="plotly_dark",
    )
    return fig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUTUSDT")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--bars", type=int, default=500)
    ap.add_argument("--concepts", default="fvg")
    ap.add_argument("--out", default="/tmp/visual_verify.html")
    args = ap.parse_args()

    concepts = {c.strip() for c in args.concepts.split(",") if c.strip()}
    fig = build_chart(args.symbol, args.tf, args.bars, concepts)
    fig.write_html(args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
