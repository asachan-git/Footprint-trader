"""Session-aware VP sourcing for hvn_inside_touch zones.

Pins the four behaviours the rewrite guarantees: 15m zone-pin across cycle TFs,
prior-day-until-warmed in Asia, range-bracket (not VA) prior-day selection, and
today-exclusion so a prior-day borrow never re-borrows today.
"""

import execution.zone_triggers as zt


# ── prior-day selection: RANGE bracket, today excluded ───────────────────────

def _hist(*days):
    """days: (key, range_lo, range_hi, va_lo, va_hi, hvn_low) tuples, oldest first."""
    return [{"period_key": k, "price_range_low": rl, "price_range_high": rh,
             "val": vl, "vah": vh, "hvn_zones": [{"low": hl, "high": hl + 1.0}]}
            for (k, rl, rh, vl, vh, hl) in days]


def test_prev_day_selects_on_full_range_not_value_area(monkeypatch):
    # price 4100 is inside day-B's RANGE (4080-4140) but OUTSIDE its VA (4110-4130).
    # Range-bracket must pick day-B; a VA-bracket would skip it.
    hist = _hist(
        ("2026-07-22", 4000.0, 4050.0, 4010.0, 4040.0, 4020.0),   # range misses 4100
        ("2026-07-23", 4080.0, 4140.0, 4110.0, 4130.0, 4085.0),   # range holds 4100, VA doesn't
    )
    monkeypatch.setattr(zt, "_prev_day_hvn_asof", zt._prev_day_hvn_asof)  # ensure real fn
    import pipeline.features.vp_cache as vc
    monkeypatch.setattr(vc, "get_history", lambda s, p, n=5: hist)
    monkeypatch.setattr(vc, "today_day_key", lambda s, now_ts=None: "2026-07-24")

    z = zt._prev_day_hvn_asof("SYM", price=4100.0, bracket="range")
    assert z == [(4085.0, 4086.0)]                       # day-B's node

    z_va = zt._prev_day_hvn_asof("SYM", price=4100.0, bracket="va")
    assert z_va == []                                    # VA of neither day holds 4100


def test_prev_day_excludes_today(monkeypatch):
    # today's entry has a node; it must be ignored so we borrow YESTERDAY, not today.
    hist = _hist(
        ("2026-07-23", 4080.0, 4140.0, 4090.0, 4130.0, 4085.0),
        ("2026-07-24", 4080.0, 4140.0, 4090.0, 4130.0, 4999.0),   # today — must be skipped
    )
    import pipeline.features.vp_cache as vc
    monkeypatch.setattr(vc, "get_history", lambda s, p, n=5: hist)
    monkeypatch.setattr(vc, "today_day_key", lambda s, now_ts=None: "2026-07-24")

    z = zt._prev_day_hvn_asof("SYM", price=4100.0, bracket="range")
    assert z == [(4085.0, 4086.0)]                       # yesterday, NOT the 4999 today-node


def test_prev_day_no_price_returns_most_recent_prior(monkeypatch):
    hist = _hist(
        ("2026-07-22", 4000.0, 4050.0, 4010.0, 4040.0, 4020.0),
        ("2026-07-23", 4080.0, 4140.0, 4090.0, 4130.0, 4085.0),
        ("2026-07-24", 4080.0, 4140.0, 4090.0, 4130.0, 4999.0),   # today
    )
    import pipeline.features.vp_cache as vc
    monkeypatch.setattr(vc, "get_history", lambda s, p, n=5: hist)
    monkeypatch.setattr(vc, "today_day_key", lambda s, now_ts=None: "2026-07-24")

    z = zt._prev_day_hvn_asof("SYM")                      # no price → newest prior day
    assert z == [(4085.0, 4086.0)]                       # 07-23, not today's 07-24


# ── outside-range vs outside-VA ──────────────────────────────────────────────

def test_outside_daily_range_uses_full_range(monkeypatch):
    import pipeline.features.vp_cache as vc
    monkeypatch.setattr(vc, "get", lambda s, p: {
        "price_range_low": 4000.0, "price_range_high": 4100.0,
        "val": 4030.0, "vah": 4070.0})
    # 4085 is inside the range but outside the VA — range check False, VA check True.
    assert zt._outside_daily_range("SYM", 4085.0) is False
    assert zt._outside_daily_va("SYM", 4085.0) is True
    # 4120 is outside both.
    assert zt._outside_daily_range("SYM", 4120.0) is True


# ── session routing ──────────────────────────────────────────────────────────

class _Bar:
    def __init__(self, c, ts):
        self.ohlc = type("O", (), {"c": c, "h": c, "l": c})()
        self.close_ts = ts


def _route(monkeypatch, session, *, warmed=True, price=4100.0,
           outside_range=False, cfg=None):
    """Drive _session_hvn_zones with each source stubbed to a distinct sentinel so the
    returned set reveals which sources were composed."""
    monkeypatch.setattr(zt, "_zone_cfg", lambda: cfg or {"vp_session_warmup_bars": 8,
                                                          "overlap_like": "london"})
    import pipeline.features.session as se
    monkeypatch.setattr(se, "current_session",
                        lambda ts=None, sym=None: type("S", (), {"session": session})())
    monkeypatch.setattr(zt, "_session_bars_15m", lambda s, n=None: 99 if warmed else 0)
    monkeypatch.setattr(zt, "_cached_hvn", lambda s: [(1.0, 1.5)])            # TODAY
    monkeypatch.setattr(zt, "_rolling_hvn_zonetf", lambda s: [(2.0, 2.5)])    # ROLLING
    monkeypatch.setattr(zt, "_prev_day_hvn_asof",
                        lambda s, price=None, bracket="range": [(3.0, 3.5)])  # PRIOR
    monkeypatch.setattr(zt, "_outside_daily_range", lambda s, p: outside_range)
    monkeypatch.setattr(zt, "_outside_daily_va", lambda s, p: False)
    z, sess = zt._session_hvn_zones("SYM", "5m", [_Bar(price, 1_000_000)])
    return set(z), sess


def test_asia_warmed_uses_today(monkeypatch):
    z, _ = _route(monkeypatch, "Asia", warmed=True)
    assert z == {(1.0, 1.5)}                              # today's cached only


def test_asia_cold_uses_prior_day(monkeypatch):
    z, _ = _route(monkeypatch, "Asia", warmed=False)
    assert z == {(3.0, 3.5)}                              # prior-day only


def test_london_is_prior_plus_rolling(monkeypatch):
    z, _ = _route(monkeypatch, "London")
    assert z == {(3.0, 3.5), (2.0, 2.5)}                  # prior + rolling


def test_ny_is_rolling_only_inside_range(monkeypatch):
    z, _ = _route(monkeypatch, "NY", outside_range=False)
    assert z == {(2.0, 2.5)}                              # rolling only


def test_ny_adds_prior_day_beyond_range(monkeypatch):
    z, _ = _route(monkeypatch, "NY", outside_range=True)
    assert z == {(2.0, 2.5), (3.0, 3.5)}                  # rolling + prior-day (range)


def test_overlap_defaults_to_london(monkeypatch):
    z, _ = _route(monkeypatch, "Overlap")
    assert z == {(3.0, 3.5), (2.0, 2.5)}                  # like London


def test_overlap_can_be_ny(monkeypatch):
    z, _ = _route(monkeypatch, "Overlap", outside_range=False,
                  cfg={"vp_session_warmup_bars": 8, "overlap_like": "ny"})
    assert z == {(2.0, 2.5)}                              # like NY (rolling only)


def test_off_uses_today_cached(monkeypatch):
    z, _ = _route(monkeypatch, "Off")
    assert z == {(1.0, 1.5)}                              # today's cached
