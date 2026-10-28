"""halted_criterion_met.met_at_head releases only halted runs whose landing is at HEAD with clean withheld paths."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import halted_criterion_met
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20200101T000000-abc123"
OTHER = "ask-20200101T000001-def456"
DELIVERABLE = "deliv-xyz"
WITHHELD = "src/held.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path: Path, run: str = RUN, withheld=(WITHHELD,)) -> Path:
    root = tmp_path / "repo"
    (root / "state" / "warp" / run).mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    marker = {"status": "halted", "run_artifacts": {"withheld": {p: "r" for p in withheld}}}
    (root / "state" / "warp" / run / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    (root / "state" / "warp" / run / "run.json").write_text(
        json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8"
    )
    (root / "state" / "sizings" / "s.yaml").write_text(f"deliverable_id: {DELIVERABLE}\n", encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _commit(root: Path, message: str) -> str:
    (root / "seed.txt").write_text(message + "\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", message)
    return _git(root, "rev-parse", "HEAD")


def _clean() -> set:
    return set()


def test_landing_naming_the_run_id_releases_with_that_sha(tmp_path):
    root = _repo(tmp_path)
    sha = _commit(root, f"land C1 (run {RUN})\n\nInline-Review: {RUN}")
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {RUN: sha}


def test_landing_with_only_the_sizing_deliverable_id_releases(tmp_path):
    root = _repo(tmp_path)
    sha = _commit(root, f"land by hand\n\nInline-Review: other-review\nDeliverable-Id: {DELIVERABLE}")
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {RUN: sha}


def test_halt_record_and_checkpoint_commits_hold(tmp_path):
    root = _repo(tmp_path)
    _commit(root, f"warp: record halted {RUN}")
    _commit(
        root,
        f"checkpoint(wave 0): 1 rows — C1\n\nCheckpoint-Plan: docs/plans/p.md\nCheckpoint-Base: abc1234\n"
        f"Lands-Chunks: C1\nRun: {RUN}",
    )
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {}


def test_dirty_withheld_path_holds(tmp_path):
    root = _repo(tmp_path)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    assert halted_criterion_met.met_at_head(root, [RUN], lambda: {WITHHELD}) == {}


def test_landing_on_an_unreachable_branch_holds(tmp_path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-q", "-c", "side")
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    _git(root, "switch", "-q", "--detach", base)
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {}


def test_three_holders_cost_one_git_call_and_zero_holders_cost_none(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    for extra in ("ask-20200101T000002-111111", "ask-20200101T000003-222222"):
        (root / "state" / "warp" / extra).mkdir(parents=True)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    calls = _count_git(monkeypatch)
    out = halted_criterion_met.met_at_head(
        root, [RUN, "ask-20200101T000002-111111", "ask-20200101T000003-222222"], _clean
    )
    assert list(out) == [RUN] and len(calls) == 1
    calls.clear()
    assert halted_criterion_met.met_at_head(root, [], _clean) == {}
    assert calls == []


@pytest.mark.parametrize("mode", ["raises", "nonzero"])
def test_git_failure_holds(tmp_path, monkeypatch, mode):
    root = _repo(tmp_path)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    if mode == "raises":
        def boom(*a, **k):
            raise OSError("no git")

        monkeypatch.setattr(git_run, "run_git", boom)
    else:
        monkeypatch.setattr(
            git_run, "run_git", lambda *a, **k: git_run.GitResult(128, "", "fatal", False)
        )
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {}


def _count_git(monkeypatch) -> list:
    calls = []
    real = git_run.run_git

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(git_run, "run_git", counting)
    return calls


def test_dirty_withheld_holder_costs_no_git_read(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    calls = _count_git(monkeypatch)
    assert halted_criterion_met.met_at_head(root, [RUN], lambda: {WITHHELD}) == {}
    assert calls == []


def test_dirty_is_read_once_before_the_git_log(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    sha = _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    order = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: order.append("git") or real(*a, **k))
    assert halted_criterion_met.met_at_head(root, [RUN], lambda: order.append("dirty") or set()) == {RUN: sha}
    assert order == ["dirty", "git"]


def test_holder_with_nothing_withheld_never_calls_dirty(tmp_path):
    root = _repo(tmp_path, withheld=())
    sha = _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    called = []
    assert halted_criterion_met.met_at_head(root, [RUN], lambda: called.append(1) or set()) == {RUN: sha}
    assert called == []


def test_dirty_failure_holds_every_holder(tmp_path):
    root = _repo(tmp_path)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")

    def boom():
        raise OSError("status failed")

    assert halted_criterion_met.met_at_head(root, [RUN], boom) == {}


def test_another_runs_landing_naming_the_holder_in_prose_holds(tmp_path):
    root = _repo(tmp_path)
    _commit(root, f"land (run {OTHER})\n\nInline-Review: {OTHER}\n\nThis follows the halt of {RUN}.")
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {}


def test_corrupt_halt_marker_holds_the_run(tmp_path):
    root = _repo(tmp_path)
    _commit(root, f"land (run {RUN})\n\nInline-Review: {RUN}")
    assert list(halted_criterion_met.met_at_head(root, [RUN], _clean)) == [RUN]
    (root / "state" / "warp" / RUN / "halted.json").write_text("{not json", encoding="utf-8")
    assert halted_criterion_met.met_at_head(root, [RUN], _clean) == {}


def test_corrupt_halt_marker_refuses_with_the_run_named(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run_route

    root = _repo(tmp_path)
    (root / "state" / "warp" / RUN / "halted.json").write_text("{not json", encoding="utf-8")
    refusal = halted_run_route.halted_refusal(root, RUN, "terminal commit")
    assert refusal is not None and f"unreadable-halt-record:{RUN}" in refusal
    assert halted_run_route.halted_refusal(root, OTHER, "terminal commit") is None, "absent marker is no run"
    assert halted_run_route.land_only_commit_route(root, RUN) is None
