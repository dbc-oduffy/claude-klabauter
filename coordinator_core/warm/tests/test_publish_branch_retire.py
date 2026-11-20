"""Publish-branch retirement through the default `sweep_repos` run, against a tmp_path bare remote."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.warm import publish_branch_retire, push_cadence, telemetry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    svc = tmp_path / "svc"
    monkeypatch.setattr(telemetry, "svc_dir", lambda engine_root=None: svc)
    yield
    push_cadence.reset_cadence_for_test()


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    _git(["add", "--", f"{name}.txt"], repo)
    _git(["commit", "-q", "-m", name], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _remote_refs(bare: Path) -> "set[str]":
    out = _git(["for-each-ref", "--format=%(refname)"], bare)
    return set(out.splitlines())


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "origin.git"
        self.bare.mkdir()
        _git(["init", "-q", "--bare", str(self.bare)], tmp_path)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)
        _git(["config", "user.email", "t@t.example"], self.repo)
        _git(["config", "user.name", "t"], self.repo)
        _git(["remote", "add", "origin", str(self.bare)], self.repo)
        self.c1 = _commit(self.repo, "c1")
        self.c2 = _commit(self.repo, "c2")
        _git(["branch", "-m", "work/day"], self.repo)
        _git(["push", "-q", "-u", "origin", "work/day"], self.repo)
        # An unmerged side commit and a main-only commit, both off c1.
        _git(["checkout", "-q", "-b", "side", self.c1], self.repo)
        self.unmerged = _commit(self.repo, "unmerged")
        _git(["checkout", "-q", "-b", "mainline", self.c1], self.repo)
        self.on_main = _commit(self.repo, "on-main")
        _git(["checkout", "-q", "work/day"], self.repo)
        self.push(self.c1, "refs/heads/publish/" + self.c1)
        self.push(self.c2, "refs/heads/publish/" + self.c2)
        self.push(self.unmerged, "refs/heads/publish/" + self.unmerged)
        self.push(self.on_main, "refs/heads/main")
        self.push(self.on_main, "refs/heads/publish/" + self.on_main)
        self.push(self.c1, "refs/preserve/keep")
        self.push(self.c1, "refs/heads/work/old")

    def push(self, sha: str, ref: str) -> None:
        _git(["push", "-q", "origin", f"{sha}:{ref}"], self.repo)

    def sweep(self, **kw) -> None:
        push_cadence.sweep_repos([self.repo], clock=lambda: 0.0, **kw)


def _publish(refs: "set[str]") -> "set[str]":
    return {r for r in refs if r.startswith("refs/heads/publish/")}


def test_merged_publish_branches_are_deleted_and_unmerged_survives(tmp_path):
    w = World(tmp_path)
    before = _remote_refs(w.bare)
    assert len(_publish(before)) == 4

    w.sweep()

    after = _remote_refs(w.bare)
    assert _publish(after) == {"refs/heads/publish/" + w.unmerged}


def test_deletes_beyond_the_per_call_cap_wait_for_a_later_call(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(publish_branch_retire, "MAX_RETIRED_PER_CALL", 2)
    outcome = publish_branch_retire.retire_merged_publish_branches(w.repo, "origin", budget_secs=30.0)
    assert outcome.error is None
    assert len(outcome.deleted) == 2
    assert len(_publish(_remote_refs(w.bare))) == 2


def test_work_branches_and_preserve_refs_are_never_deleted(tmp_path):
    w = World(tmp_path)
    w.sweep()
    after = _remote_refs(w.bare)
    assert {"refs/heads/work/old", "refs/heads/work/day", "refs/heads/main", "refs/preserve/keep"} <= after


def test_one_batched_delete_push_per_repo(tmp_path, monkeypatch):
    w = World(tmp_path)
    pushes = []
    real = git_native._git

    def spy(args, **kw):
        if args and args[0] == "push":
            pushes.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_native, "_git", spy)
    w.sweep()
    delete_pushes = [p for p in pushes if any(a.startswith(":refs/heads/publish/") for a in p)]
    assert len(delete_pushes) == 1
    assert sum(a.startswith(":") for a in delete_pushes[0]) == 3
    assert all(
        not a.startswith((":refs/heads/work/", ":refs/preserve/", ":refs/heads/main"))
        for a in delete_pushes[0]
    )


def test_second_sweep_inside_the_interval_does_no_remote_round_trip(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.sweep()
    calls = []
    real = git_native._git
    monkeypatch.setattr(
        git_native, "_git", lambda args, **kw: (calls.append(list(args)), real(args, **kw))[1]
    )
    w.sweep()
    assert not any(a and a[0] == "ls-remote" for a in calls)


def test_unreachable_remote_logs_and_never_raises(tmp_path):
    w = World(tmp_path)
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], w.repo)
    before = _remote_refs(w.bare)

    w.sweep()

    assert _remote_refs(w.bare) == before
    log = (w.repo / ".git" / "push-failures.log").read_text(encoding="utf-8")
    assert "publish-retire-failed" in log


def test_rejected_delete_push_is_reported_not_raised(tmp_path):
    w = World(tmp_path)
    (w.bare / "hooks").mkdir(exist_ok=True)
    hook = w.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    before = _remote_refs(w.bare)

    outcome = publish_branch_retire.retire_merged_publish_branches(w.repo, "origin", budget_secs=20.0)

    assert outcome.deleted == ()
    assert outcome.error is not None and "delete push failed" in outcome.error
    assert _remote_refs(w.bare) == before


def test_tip_missing_locally_is_never_deleted(tmp_path):
    w = World(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git(["init", "-q"], other)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["remote", "add", "origin", str(w.bare)], other)
    # `other` holds no object of the remote: nothing can be proven, nothing is deleted.
    before = _remote_refs(w.bare)
    outcome = publish_branch_retire.retire_merged_publish_branches(other, "origin", budget_secs=20.0)
    assert outcome.deleted == ()
    assert _remote_refs(w.bare) == before


def test_branch_moved_since_listing_survives_the_lease(tmp_path, monkeypatch):
    w = World(tmp_path)
    real = git_native._git
    moved = {"done": False}

    def spy(args, **kw):
        if args and args[0] == "push" and not moved["done"]:
            moved["done"] = True
            _git(["update-ref", "refs/heads/publish/" + w.c1, w.on_main], w.bare)
        return real(args, **kw)

    monkeypatch.setattr(git_native, "_git", spy)
    publish_branch_retire.retire_merged_publish_branches(w.repo, "origin", budget_secs=20.0)
    assert "refs/heads/publish/" + w.c1 in _remote_refs(w.bare)
