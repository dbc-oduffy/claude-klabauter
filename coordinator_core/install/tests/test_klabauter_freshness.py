"""Table tests for the pure klabauter freshness interface."""

from pathlib import Path

import pytest

from coordinator_core.install import klabauter_freshness as kf
from coordinator_core.warm import skew


@pytest.mark.parametrize(
    "args, action",
    [
        (("a", "a", 0, 0, False, 0), kf.ACTION_CURRENT),
        (("a", "a", 0, 0, True, 3), kf.ACTION_CURRENT),
        (("a", "b", 2, 0, False, 0), kf.ACTION_CURRENT),
        (("a", "b", 0, 3, True, 0), kf.ACTION_SURFACE_DIRTY),
        (("a", "b", 1, 3, True, 0), kf.ACTION_SURFACE_DIRTY),
        (("a", "b", 1, 3, False, 0), kf.ACTION_SURFACE_DIVERGED),
        (("a", "b", 1, 3, False, 2), kf.ACTION_SURFACE_DIVERGED),
        (("a", "b", 0, 3, False, 1), kf.ACTION_DEFERRED_LIVE_RUN),
        (("a", "b", 0, 3, False, 0), kf.ACTION_FAST_FORWARD),
    ],
)
def test_decide_table(args, action):
    verdict = kf.decide(*args)
    assert verdict.action == action
    assert verdict.action in kf.ACTIONS
    assert verdict.reason


def test_stale_installs_independent_of_ref_verdict():
    src = {"relay": "h2", "bridge": "h1", "roster": "h3"}
    inst = {"relay": "h1", "bridge": "h1", "extra": "zz"}
    assert kf.stale_installs(src, inst) == ("relay", "roster")
    assert kf.stale_installs(src, src) == ()
    assert kf.stale_installs({"a": "x"}, {"a": None}) == ("a",)


def test_installers_for_map():
    assert kf.installers_for([]) == ()
    assert kf.installers_for(["docs/x.md"]) == ()
    assert kf.installers_for(["coordinator/bin/unimatrix-relay.py"]) == (
        kf.INSTALLER_UNIMATRIX,
        kf.INSTALLER_CURRENCY_CACHE,
    )
    assert kf.installers_for(["templates/bin/coordinator-x"]) == (kf.INSTALLER_BIN_SHIMS,)
    assert kf.installers_for(["coordinator_core/a.py"]) == (kf.INSTALLER_CURRENCY_CACHE,)
    # not a unimatrix script: wrong dir / wrong suffix
    assert kf.INSTALLER_UNIMATRIX not in kf.installers_for(["coordinator/bin/tests/unimatrix-x.py"])
    assert kf.INSTALLER_UNIMATRIX not in kf.installers_for(["coordinator/bin/unimatrix-x.md"])
    assert kf.installers_for(
        ["templates/bin/b", "coordinator/bin/unimatrix-roster.py"]
    ) == (kf.INSTALLER_UNIMATRIX, kf.INSTALLER_BIN_SHIMS, kf.INSTALLER_CURRENCY_CACHE)


def test_engine_touching_paths_match_skew():
    assert kf.ENGINE_TOUCHING_PATHS == skew._ENGINE_TOUCHING_PATHS


def test_surfaced_key_record_roundtrip_and_once():
    key = kf.SurfacedKey("box", "aaa", "bbb", kf.STATE_DIRTY)
    rec = kf.key_to_record(key)
    assert rec == {"hostname": "box", "local_sha": "aaa", "origin_sha": "bbb", "state": "dirty"}
    assert kf.key_from_record(rec) == key
    assert not kf.is_new_surface(rec, key)
    assert kf.is_new_surface(None, key)
    assert kf.is_new_surface(rec, key._replace(origin_sha="ccc"))
    assert kf.is_new_surface(rec, key._replace(state=kf.STATE_DIVERGED))
    assert kf.key_from_record({"hostname": "box"}) is None
    assert kf.key_from_record({**rec, "state": 3}) is None
    assert kf.key_from_record("x") is None


def test_record_path_and_surfaced_state():
    assert kf.freshness_record_path(Path("h")) == Path("h") / "coordinator" / "klabauter-freshness.json"
    assert kf.surfaced_state_for(kf.ACTION_SURFACE_DIRTY) == kf.STATE_DIRTY
    assert kf.surfaced_state_for(kf.ACTION_SURFACE_DIVERGED) == kf.STATE_DIVERGED
    assert kf.surfaced_state_for(kf.ACTION_CURRENT) is None
