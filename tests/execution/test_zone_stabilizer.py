"""_stabilize_zones hysteresis: jittered edges stay put, flicker-absent zones survive
del_n-1 misses, merges/splits keep outer edges stable, real migrations pass through."""

import pytest

from execution import zone_triggers as zt


BIN = 0.5  # test bin size (monkeypatched below)
SESS = "ny"
SYM, TF = "TESTSYM", "5m"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    zt._ZONE_STAB.clear()
    # deterministic config — bypass the yaml read
    monkeypatch.setitem(zt._STAB_CFG_CACHE, "cfg", {
        "hvn_stab_enabled": True,
        "hvn_stab_edge_tol_frac": 0.25,
        "hvn_stab_del_n": 3,
    })
    monkeypatch.setitem(zt._STAB_CFG_CACHE, "ts", 1e18)  # never expire during test
    import pipeline.features.volume_profile as vp
    monkeypatch.setattr(vp, "_resolve_bin_size", lambda s: BIN)
    yield
    zt._ZONE_STAB.clear()


def stab(zones):
    return zt._stabilize_zones(SYM, TF, zones, SESS)


def test_first_call_passthrough():
    assert stab([(100.0, 110.0)]) == [(100.0, 110.0)]


def test_edge_jitter_snapped():
    stab([(100.0, 110.0)])
    # ±1 bin wiggle (< tol = max(0.25×10, 2×0.5) = 2.5) → old edges kept
    assert stab([(100.5, 109.5)]) == [(100.0, 110.0)]
    assert stab([(99.6, 110.4)]) == [(100.0, 110.0)]


def test_real_migration_accepted():
    stab([(100.0, 110.0)])
    # edge moved 4.0 > tol 2.5 → accept new edge; other edge within tol → snapped
    assert stab([(104.0, 110.5)]) == [(104.0, 110.0)]


def test_flicker_absence_survives_then_dies():
    stab([(100.0, 110.0), (120.0, 125.0)])
    # zone 2 flickers out — survives (miss 1, 2), dies on 3rd consecutive absence
    assert stab([(100.0, 110.0)]) == [(100.0, 110.0), (120.0, 125.0)]
    assert stab([(100.0, 110.0)]) == [(100.0, 110.0), (120.0, 125.0)]
    assert stab([(100.0, 110.0)]) == [(100.0, 110.0)]


def test_reappearance_resets_miss_count():
    stab([(100.0, 110.0), (120.0, 125.0)])
    stab([(100.0, 110.0)])                    # miss 1
    stab([(100.0, 110.0), (120.0, 125.0)])    # back → reset
    stab([(100.0, 110.0)])                    # miss 1 again
    assert stab([(100.0, 110.0)]) == [(100.0, 110.0), (120.0, 125.0)]  # miss 2, alive


def test_new_zone_appears_immediately():
    stab([(100.0, 110.0)])
    assert stab([(100.0, 110.0), (150.0, 155.0)]) == [(100.0, 110.0), (150.0, 155.0)]


def test_merge_keeps_outer_edges():
    stab([(100.0, 110.0), (111.0, 118.0)])
    # one wide zone spanning both — outer edges wiggle within tol → snapped to old outers
    assert stab([(100.6, 117.5)]) == [(100.0, 118.0)]


def test_split_outer_edges_stable():
    stab([(100.0, 118.0)])
    # split into two: each matches the old wide zone by overlap; each new zone's edge
    # near an old OUTER edge snaps, interior boundary accepted as-is
    out = stab([(100.5, 107.0), (112.0, 117.6)])
    assert out[0][0] == 100.0        # outer low snapped
    assert out[-1][1] == 118.0       # outer high snapped
    assert out[0][1] == 107.0        # interior split boundary accepted


def test_session_change_reseeds():
    stab([(100.0, 110.0)])
    out = zt._stabilize_zones(SYM, TF, [(101.0, 109.0)], "asia")
    assert out == [(101.0, 109.0)]   # fresh snapshot, no snapping


def test_disabled_passthrough():
    zt._STAB_CFG_CACHE["cfg"] = {"hvn_stab_enabled": False}
    stab([(100.0, 110.0)])
    assert stab([(100.5, 109.5)]) == [(100.5, 109.5)]
