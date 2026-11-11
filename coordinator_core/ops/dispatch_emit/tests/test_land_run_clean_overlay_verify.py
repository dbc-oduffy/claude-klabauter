"""Falsifier: the landing set's scoped tests run in a shared clone of the candidate commit (verify stage), so
peer dirt in the worktree neither fails nor greens them; land_run only reads the keyed verdict, landing on green,
inconclusive or missing (the last two with an advisory) and refusing on red or a mismatched key, and never runs a
test itself."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (
    FIXTURE,
    LANDING,
    _VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

READER = "t/test_reader.py"
_READER_SRC = (
    "import pathlib\n\n\ndef test_reads_data():\n"
    "    assert pathlib.Path(__file__).with_name('data.txt').read_text().strip() == 'ok'\n"
)


def _repo(tmp_path, *, data: str = "ok\n"):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_creationflags())
    _write(root, READER, _READER_SRC)
    _write(root, "t/data.txt", data)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD").strip()


def test_peer_dirt_in_the_worktree_does_not_fail_the_overlay(tmp_path):
    root, head = _repo(tmp_path)
    _write(root, "t/data.txt", "peer dirt\n")

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "green", out
    assert not cov.overlay_root(root, "run1").exists()


def test_in_repo_overlay_holder_is_ledgered_for_the_run_end_reap(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_scratch

    root, head = _repo(tmp_path)

    cov.verify(root, "run1", head, [READER])

    assert "scratch/clean-overlay/run1" in run_scratch.read_ledger(root, "run1")["paths"]


def test_out_of_repo_overlay_holder_is_not_ledgered(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_scratch

    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")

    cov.verify(root, "run1", head, [READER])

    assert run_scratch.read_ledger(root, "run1")["paths"] == []


def test_a_red_in_the_candidate_is_red_even_when_the_worktree_is_clean(tmp_path):
    root, head = _repo(tmp_path, data="broken\n")

    out = cov.verify(root, "run1", head, [READER])

    assert out.red, out
    assert not cov.overlay_root(root, "run1").exists()


def test_overlay_clone_keeps_the_live_repo_directory_name(tmp_path):
    root, head = _repo(tmp_path)
    _write(
        root, "t/test_name.py",
        "import pathlib\n\n\ndef test_name():\n"
        "    assert pathlib.Path(__file__).resolve().parents[1].name == 'repo'\n",
    )
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "name")
    head = _git(root, "rev-parse", "HEAD").strip()

    out = cov.verify(root, "run-id-not-repo", head, ["t/test_name.py"])

    assert out.verdict == "green", out
    assert not cov.overlay_root(root, "run-id-not-repo").exists()


def _pytest_argv_head(monkeypatch, root, head, pin):
    if pin is not None:
        (root / "coordinator.local.md").write_text(f'---\ntargeted_test_python: "{pin}"\n---\n', encoding="utf-8")
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout):
        seen.append(argv[0])
        return real([sys.executable, *argv[1:]] if "pytest" in argv else argv, cwd, timeout)

    monkeypatch.setattr(cov, "_run", spy)
    cov.verify(root, "run1", head, [READER])
    return seen[-1]


def test_overlay_runs_pytest_under_the_repo_pinned_interpreter(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    shim = tmp_path / "pins" / "python3.99"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    shim.chmod(0o755)

    assert _pytest_argv_head(monkeypatch, root, head, shim) == str(shim)


def test_overlay_falls_back_to_sys_executable_when_no_pin_or_the_pin_is_unresolved(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    assert _pytest_argv_head(monkeypatch, root, head, None) == sys.executable
    assert _pytest_argv_head(monkeypatch, root, head, tmp_path / "missing" / "python3.99") == sys.executable


def test_the_overlay_checkout_runs_under_the_overlay_bound_not_the_2s_plumbing_budget(tmp_path, monkeypatch):
    """A full-tree checkout under run_git's 2s budget timed out on the live repo: rc -1, empty stderr."""
    root, head = _repo(tmp_path)
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout):
        seen.append((argv[1:2], timeout))
        return real(argv, cwd, timeout)

    monkeypatch.setattr(cov, "_run", spy)
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    assert (["checkout"], cov.TEST_TIMEOUT_S) in seen


def test_a_timed_out_overlay_checkout_is_named_with_its_budget_never_silent(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)

    def slow(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cov, "_run", slow)
    out = cov.verify(root, "run1", head, [READER])
    assert out.verdict == "inconclusive"
    assert "TimeoutExpired" in out.detail and f"{cov.TEST_TIMEOUT_S} seconds" in out.detail, out.detail


def _clone_dests(monkeypatch):
    dests = []
    real = cov.run_git

    def spy(args, **kw):
        if args[:1] == ["clone"]:
            dests.append(args[-1])
        return real(args, **kw)

    monkeypatch.setattr(cov, "run_git", spy)
    return dests


def test_a_configured_bulk_location_receives_the_overlay_clone_and_is_reaped(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")
    dests = _clone_dests(monkeypatch)

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "green", out
    assert dests == [str(bulk / "clean-overlay" / "run1" / root.resolve().name)]
    assert not (root / "scratch").exists()
    assert not (bulk / "clean-overlay" / "run1").exists()


def test_unconfigured_or_relative_bulk_falls_back_to_repo_scratch(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    expected = str(root / "scratch" / "clean-overlay" / "run1" / root.resolve().name)
    dests = _clone_dests(monkeypatch)

    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    (root / "coordinator.local.md").write_text("---\nbulk_scratch_dir: rel/dir\n---\n", encoding="utf-8")
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"

    assert dests == [expected, expected]
    assert not cov.overlay_root(root, "run1").exists()


def test_a_bulk_overlay_is_reaped_when_the_candidate_is_unreachable(tmp_path):
    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")

    assert cov.verify(root, "run1", "0" * 40, [READER]).verdict == "inconclusive"
    assert not cov.overlay_root(root, "run1").exists()


def test_no_seeds_and_an_unreachable_candidate_are_inconclusive_never_red(tmp_path):
    root, head = _repo(tmp_path)

    assert cov.verify(root, "run1", head, []).verdict == "inconclusive"
    assert cov.verify(root, "run1", "0" * 40, [READER]).verdict == "inconclusive"
    assert not cov.overlay_root(root, "run1").exists()


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("land_run must not run the overlay verify")

    monkeypatch.setattr(cov, "verify", boom)
    monkeypatch.setattr(cov, "_run", boom)


def _verdict(root, measured, verdict, **over):
    cov.write_verdict(root, RUN2, {**measured, **over}, cov.OverlayVerdict(verdict, "boom", ("t/x.py",)))


def _dirty_land(halted, verdict, *, mutate=None):
    _track_fixture(halted)
    resume, token = _stage(halted)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=halted)
    if verdict:
        _verdict(halted, measured, verdict, **(mutate or {}))
    _write(halted, FIXTURE, '{"a": 9, "dirty": true}\n')
    return measured, _terminal_with(halted, token, resume, _VERDICT)


def test_land_run_lands_on_a_green_verdict_despite_foreign_dirt(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "green")

    assert result["committed"] is True, result
    assert FIXTURE in result["reach_dirt_foreign"]
    assert result["clean_overlay_verify"]["verdict"] == "green"
    assert FIXTURE in _git(halted, "status", "--porcelain", "--", FIXTURE)


def test_land_run_refuses_a_red_verdict(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "red")

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-red", result
    assert _git(halted, "log", "-1", "--format=%s").strip() == "fixture"


def test_land_run_lands_on_an_inconclusive_verdict_with_an_advisory(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "inconclusive")

    assert result["committed"] is True, result
    assert result["clean_overlay_verify"]["verdict"] == "inconclusive"
    assert "inconclusive" in result["clean_overlay_verify"]["advisory"]
    assert _git(halted, "log", "-1", "--format=%(trailers:key=Overlay,valueonly)").strip().startswith("inconclusive"), _git(halted, "log", "-1", "--format=%B")
    assert FIXTURE in result["reach_dirt_foreign"]


def test_land_run_lands_a_missing_verdict_with_an_advisory(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, None)

    assert result["committed"] is True, result
    assert not result.get("refused")
    assert result["clean_overlay_verify"]["verdict"] == "missing"
    assert "missing" in result["clean_overlay_verify"]["advisory"]
    assert _git(halted, "log", "-1", "--format=%(trailers:key=Overlay,valueonly)").strip().startswith("missing"), _git(halted, "log", "-1", "--format=%B")
    assert FIXTURE in result["reach_dirt_foreign"]


@pytest.mark.parametrize("field", ["blobs", "base_sha"])
def test_land_run_refuses_a_verdict_keyed_to_another_landing_set_or_head(halted, monkeypatch, field):  # noqa: F811
    _no_spawn(monkeypatch)
    _track_fixture(halted)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=halted)
    other = {"blobs": {"x": "0" * 40}} if field == "blobs" else {"base_sha": "1" * 40}
    assert cov.check_verdict(halted, RUN2, measured).verdict == "missing"
    _verdict(halted, measured, "green", **other)

    out = cov.check_verdict(halted, RUN2, measured)

    assert out.verdict == "mismatch", out


@pytest.mark.parametrize("field", ["blobs", "base_sha"])
def test_land_run_refuses_a_mismatched_verdict_at_land(halted, monkeypatch, field):  # noqa: F811
    _no_spawn(monkeypatch)
    other = {"blobs": {"x": "0" * 40}} if field == "blobs" else {"base_sha": "1" * 40}

    _, result = _dirty_land(halted, "green", mutate=other)

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-unverified", result


def test_no_foreign_dirt_needs_no_verdict_and_runs_no_overlay(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)
    _track_fixture(halted)
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, LANDING, repo_root=halted)

    result = _terminal_with(halted, token, resume, _VERDICT)

    assert result["committed"] is True, result


def test_cli_writes_a_verdict_land_run_reads(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    run = "warp-2026-11-01-cli-abc123"
    record = cov.measured_rev_record_path(root, run)
    record.parent.mkdir(parents=True, exist_ok=True)
    measured = {"sha": head, "base_sha": head, "paths": [READER], "blobs": {READER: "b"}}
    record.write_text(json.dumps(measured), encoding="utf-8")
    _write(root, "t/data.txt", "peer dirt\n")

    assert cov.main(["--run-id", run, "--repo-root", str(root)]) == 0

    assert cov.check_verdict(root, run, measured).verdict == "green"
    assert not cov.overlay_root(root, run).exists()


def test_reading_the_verdict_stays_inside_the_brightline(tmp_path):
    measured = {"sha": "a" * 40, "base_sha": "b" * 40, "blobs": {"p": "c" * 40}}
    cov.write_verdict(tmp_path, RUN2, measured, cov.OverlayVerdict("green", "", ("t.py",)))
    start = time.process_time()
    for _ in range(20):
        assert cov.check_verdict(tmp_path, RUN2, measured).verdict == "green"
    assert (time.process_time() - start) / 20 < 0.05
