"""A red clean-overlay verdict whose failing nodes all sit in tests outside the landing set that reach an
uncommitted path another live run claims lands with an `Overlay: red` advisory naming the claimant; a failing
own-seed node, or a reach path no run claims, still refuses `verify-overlay-red`. Driven through the real
producer (`measured_rev`), a written verdict file and the default `land_run._handler`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (
    FIXTURE,
    LANDING,
    _VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import LANE_TEST
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CLAIMANT = "warp-2026-11-24-claimant-holder-fe9680"
SYNTH = "synthpkg/synth.py"
IMPORTER_TEST = "synthpkg/test_importer.py"
IMPORTER_NODE = f"{IMPORTER_TEST}::test_uses_synth"
OWN_NODE = f"{LANE_TEST}::test_lane"


def _track_importer(root) -> None:
    """A tracked test outside the landing set that imports a tracked module the landing seeds do not reach."""
    _write(root, "synthpkg/__init__.py", "")
    _write(root, SYNTH, "VALUE = 1\n")
    _write(root, IMPORTER_TEST, "from synthpkg import synth\n\n\ndef test_uses_synth():\n    assert synth.VALUE == 1\n")
    _git(root, "add", "synthpkg")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "importer")


def _claim(monkeypatch, claimed: set) -> None:
    """Another live run claims exactly `claimed`; nothing is halted-withheld."""
    monkeypatch.setattr(
        land_run, "_peer_held", lambda root, paths, *a, **k: {p: [CLAIMANT] for p in paths if p in claimed}
    )
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, run_id, plan_rel, paths, holders_out=None: {})


def _land_red(halted, failing: tuple) -> dict:
    """The landing's overlay verdict is red on `failing`; the fixture the landing seeds reach and the synth module
    only the importer test reaches are both uncommitted."""
    _track_fixture(halted)
    _track_importer(halted)
    resume, token = _stage(halted)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=halted)
    cov.write_verdict(halted, RUN2, measured, cov.OverlayVerdict("red", "boom", (LANE_TEST,), failing=failing))
    _write(halted, FIXTURE, '{"a": 9, "dirty": true}\n')
    _write(halted, SYNTH, "VALUE = 2\n")
    return _terminal_with(halted, token, resume, _VERDICT)


def test_a_red_whose_failing_node_reaches_only_a_foreign_claimed_path_lands_naming_the_claimant(halted, monkeypatch):  # noqa: F811
    _claim(monkeypatch, {FIXTURE, SYNTH})

    result = _land_red(halted, (IMPORTER_NODE,))

    assert result["committed"] is True, result
    assert not result.get("refused"), result
    assert CLAIMANT in result["clean_overlay_verify"]["advisory"], result
    trailer = _git(halted, "log", "-1", "--format=%(trailers:key=Overlay,valueonly)").strip()
    assert trailer.startswith("red") and CLAIMANT in trailer, _git(halted, "log", "-1", "--format=%B")
    assert SYNTH in _git(halted, "status", "--porcelain", "--", SYNTH)


def test_red_is_foreign_runs_pins_both_sides_of_the_reach_attribution():
    held = {"a.py": CLAIMANT, "b.py": "unattributed"}
    reach = {"t/test_x.py": {"a.py": "blob"}, "t/test_y.py": {"a.py": "blob", "b.py": "blob"}, "t/test_z.py": {}}

    assert land_run._red_is_foreign_runs(["t/test_x.py::t"], held, reach, ["own.py"]) is True
    assert land_run._red_is_foreign_runs(["t/test_y.py::t"], held, reach, ["own.py"]) is False
    assert land_run._red_is_foreign_runs(["t/test_z.py::t"], held, reach, ["own.py"]) is False
    assert land_run._red_is_foreign_runs(["t/test_x.py::t"], held, reach, ["t/test_x.py"]) is False
    assert land_run._red_is_foreign_runs(["t/test_x.py::t"], held) is False


def test_a_failing_own_seed_node_still_refuses_verify_overlay_red(halted, monkeypatch):  # noqa: F811
    _claim(monkeypatch, {FIXTURE, SYNTH})

    result = _land_red(halted, (IMPORTER_NODE, OWN_NODE))

    assert result["committed"] is False, result
    assert result["refused"] == land_run.VERIFY_OVERLAY_RED, result


def test_a_reach_path_no_run_claims_still_refuses_verify_overlay_red(halted, monkeypatch):  # noqa: F811
    _claim(monkeypatch, {FIXTURE})

    result = _land_red(halted, (IMPORTER_NODE,))

    assert result["committed"] is False, result
    assert result["refused"] == land_run.VERIFY_OVERLAY_RED, result
