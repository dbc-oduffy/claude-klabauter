"""Falsifier: land_run refuses a landing whose verified tests can read a dirty tracked fixture outside the
landing set, whether unheld or held by a live peer run, and lands when the fixture is clean; driven through
the real producer (`measured_rev`) and the default `land_run._handler`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import (
    C3_PATH,
    C4_PATH,
    LANE,
    LANE_TEST,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": ["C3"]}]}
LANDING = [C3_PATH, C4_PATH, LANE, LANE_TEST]
FIXTURE = "x/fixtures/data.json"
PEER = "warp-2026-11-01-peer-holder-abc123"


def _track_fixture(root) -> None:
    _write(root, FIXTURE, '{"a": 1}\n')
    _git(root, "add", FIXTURE)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture")


def _hold(monkeypatch) -> None:
    monkeypatch.setattr(land_run, "_peer_held", lambda root, paths, *a, **k: {p: [PEER] for p in paths if p == FIXTURE})
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda root, run_id, plan_rel, paths, holders_out=None: {p: PEER for p in paths if p == FIXTURE},
    )


def _refused(root, head, out) -> None:
    assert out["committed"] is False and out["refused"] == land_run.VERIFY_REACH_DIRTY, out
    assert FIXTURE in out["paths"]
    assert _git(root, "rev-parse", "HEAD") == head
    assert FIXTURE in _git(root, "status", "--porcelain", "--", FIXTURE)


def test_an_unheld_dirty_fixture_in_the_lane_tests_reach_refuses_the_land(halted):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    _write(halted, FIXTURE, '{"a": 2}\n')
    mr.measured_rev(RUN2, LANDING, repo_root=halted)
    head = _git(halted, "rev-parse", "HEAD")

    _refused(halted, head, _terminal_with(halted, token, resume, _VERDICT))


def test_a_fixture_a_live_peer_run_holds_refuses_the_land_all_the_same(halted, monkeypatch):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, LANDING, repo_root=halted)
    _write(halted, FIXTURE, '{"a": 3}\n')
    _hold(monkeypatch)
    head = _git(halted, "rev-parse", "HEAD")

    _refused(halted, head, _terminal_with(halted, token, resume, _VERDICT))


def test_a_clean_fixture_in_reach_lands(halted):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, LANDING, repo_root=halted)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert out.get("refused") != land_run.VERIFY_REACH_DIRTY
