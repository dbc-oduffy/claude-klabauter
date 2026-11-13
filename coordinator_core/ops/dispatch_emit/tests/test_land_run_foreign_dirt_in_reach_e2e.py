"""Falsifier: land_run lands a green run whose verified tests can read a dirty tracked fixture outside the
landing set, naming it in `reach_dirt_foreign` (holder `unattributed` when no run claims it, the peer when one
does), and lands when the fixture is clean; driven through the real producer (`measured_rev`) and the default
`land_run._handler`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.win_portability import no_console_creationflags
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


FIXTURE2 = "x/fixtures/other.json"


def _track_fixture(root, *paths) -> None:
    for path in paths or (FIXTURE,):
        _write(root, path, '{"a": 1}\n')
        _git(root, "add", path)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture")


def _measure(root, *, overlay: bool = True) -> None:
    """The workflow's measure then verify stages: `measured_rev`, then the clean-overlay verdict writer."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    mr.measured_rev(RUN2, LANDING, repo_root=root)
    if overlay:
        assert cov.main(["--run-id", RUN2, "--repo-root", str(root)]) == 0


def _hold(monkeypatch) -> None:
    monkeypatch.setattr(land_run, "_peer_held", lambda root, paths, *a, **k: {p: [PEER] for p in paths if p == FIXTURE})
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda root, run_id, plan_rel, paths, holders_out=None: {p: PEER for p in paths if p == FIXTURE},
    )


def test_an_unheld_dirty_fixture_in_the_lane_tests_reach_lands_as_unattributed_advisory(halted):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    _write(halted, FIXTURE, '{"a": 2, "dirty": true}\n')
    _measure(halted)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert out["reach_dirt_foreign"] == {FIXTURE: "unattributed"}, out
    assert FIXTURE in _git(halted, "status", "--porcelain", "--", FIXTURE)


def test_a_fixture_a_live_peer_run_holds_lands_with_the_advisory(halted, monkeypatch):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    _measure(halted)
    _write(halted, FIXTURE, '{"a": 3, "dirty": true}\n')
    _hold(monkeypatch)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert FIXTURE in out["reach_dirt_foreign"], out
    assert FIXTURE in _git(halted, "status", "--porcelain", "--", FIXTURE)


def test_a_clean_fixture_in_reach_lands(halted):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    _measure(halted)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out


def test_one_peer_held_and_one_unheld_fixture_each_keep_their_own_holder(halted, monkeypatch):  # noqa: F811
    _track_fixture(halted, FIXTURE, FIXTURE2)
    resume, token = _stage(halted)
    _measure(halted)
    _write(halted, FIXTURE, '{"a": 4, "dirty": true}\n')
    _write(halted, FIXTURE2, '{"a": 5, "dirty": true}\n')
    _hold(monkeypatch)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert set(out["reach_dirt_foreign"]) == {FIXTURE, FIXTURE2}, out
    assert out["reach_dirt_foreign"][FIXTURE2] == "unattributed", out
    assert out["reach_dirt_foreign"][FIXTURE] != "unattributed", out


_READER_TEST = (
    "import pathlib\n\n\ndef test_lane_reads_the_clean_fixture():\n"
    "    data = pathlib.Path(__file__).parent / 'fixtures' / 'data.json'\n"
    "    assert data.read_text() == '{\"a\": 1}\\n'\n"
)
_DIRT = '{"a": 9, "peer": "dirt"}\n'


def _lane_test_reads_the_fixture(root) -> None:
    _write(root, LANE_TEST, _READER_TEST)


def test_a_landing_whose_test_would_fail_on_the_peer_dirt_lands_green_in_the_clean_overlay(halted):  # noqa: F811
    _track_fixture(halted)
    resume, token = _stage(halted)
    _lane_test_reads_the_fixture(halted)
    _measure(halted)
    _write(halted, FIXTURE, _DIRT)
    head = _git(halted, "rev-parse", "HEAD").strip()

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert FIXTURE in out["reach_dirt_foreign"], out
    assert out["clean_overlay_verify"]["verdict"] == "green", out
    assert _git(halted, "rev-parse", "HEAD").strip() != head
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert FIXTURE not in landed and LANE_TEST in landed
    assert (halted / FIXTURE).read_text(encoding="utf-8") == _DIRT
    assert FIXTURE in _git(halted, "status", "--porcelain", "--", FIXTURE)


def test_peer_dirt_in_reach_with_no_overlay_verdict_refuses_as_unverified(halted):  # noqa: F811
    """A missing verdict is unverified: the dirt-in-reach landing refuses, commits nothing, and leaves the dirt."""
    _track_fixture(halted)
    resume, token = _stage(halted)
    _lane_test_reads_the_fixture(halted)
    _measure(halted, overlay=False)
    _write(halted, FIXTURE, _DIRT)
    head = _git(halted, "rev-parse", "HEAD").strip()

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == "verify-overlay-unverified", out
    assert _git(halted, "rev-parse", "HEAD").strip() == head
    assert (halted / FIXTURE).read_text(encoding="utf-8") == _DIRT


def test_the_same_test_fails_against_the_peer_dirt_in_the_worktree(halted):  # noqa: F811
    """Control: the dirt is real, so the green above comes from the overlay's isolation."""
    import subprocess
    import sys

    _track_fixture(halted)
    _stage(halted)
    _lane_test_reads_the_fixture(halted)
    _write(halted, FIXTURE, _DIRT)

    ran = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(halted), LANE_TEST],
        cwd=str(halted), capture_output=True, text=True, **no_console_creationflags(),
    )

    assert ran.returncode == 1, ran.stdout + ran.stderr


def test_overlay_setup_process_time_and_spawn_count_are_recorded(halted, tmp_path):  # noqa: F811
    """Records clone + detached checkout cost (the overlay's setup legs); asserts spawn count only, no time bound."""
    resource = pytest.importorskip("resource")
    import json
    import time
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    _track_fixture(halted)
    head = _git(halted, "rev-parse", "HEAD").strip()
    dest = tmp_path / "overlay"
    legs = (
        ["git", "clone", "--shared", "--no-checkout", "-q", str(halted), str(dest)],
        ["git", "checkout", "--detach", "-q", head],
    )
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall = time.perf_counter()
    cwds = (halted, dest)
    for argv, cwd in zip(legs, cwds):
        assert cov._run(argv, cwd, cov.SETUP_TIMEOUT_S).returncode == 0
    wall = time.perf_counter() - wall
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = ((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)) * 1000
    record = {"spawns": len(legs), "process_ms": round(cpu_ms, 1), "wall_ms": round(wall * 1000, 1),
              "over_200ms_process": cpu_ms > 200}
    (tmp_path / "overlay-setup-cost.json").write_text(json.dumps(record), encoding="utf-8")
    print("overlay-setup-cost", json.dumps(record))

    assert (dest / FIXTURE).is_file()
