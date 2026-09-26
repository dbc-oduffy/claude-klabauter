"""
coordinator_core.ops.ceremony.tests.test_push_mismatched_upstream_name

Pins the fix for the defect measured 2026-09-22 in a cloud session (DoE-
claude clone): a bare `git push` refuses under `push.default=simple` the
moment the tracked upstream's branch NAME differs from the local branch's
own name -- the standard cloud-harness shape, a local `work/vm/<date>`
tracking a differently-named `origin/claude/<session>`:

    fatal: The upstream branch of your current branch does not match the
    name of your current branch ... see option 'simple' of
    branch.autoSetupMerge

`push_with_retry` must push by EXPLICIT refspec (`HEAD:<remote-branch>`)
whenever an upstream is configured, landing on the upstream branch
regardless of any local/remote name mismatch, and must never fall back to
the bare, refspec-less `git_native.push()` in that case.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod

pytestmark = [pytest.mark.spawns_process]

_LOCAL_BRANCH = "work/vm/2026-09-22"
_REMOTE_BRANCH = "claude/compassionate-pascal-98ncw7"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_mismatched_upstream_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", _LOCAL_BRANCH], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    # Publish once under the DIFFERENT remote name and set it as upstream --
    _git(
        ["push", "-q", "-u", "origin", f"{_LOCAL_BRANCH}:refs/heads/{_REMOTE_BRANCH}"],
        repo,
    )
    return repo


def _remote_head_sha(repo: Path, remote_branch: str) -> str:
    origin = repo.parent / "origin.git"
    proc = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{remote_branch}"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_push_lands_on_the_differently_named_upstream_branch(tmp_path, monkeypatch):
    repo = _init_mismatched_upstream_repo(tmp_path)

    (repo / "README.md").write_text("second", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()

    def _bare_push_must_not_be_called(*a, **kw):
        raise AssertionError(
            "git_native.push() (bare, refspec-less) must not be called when "
            "an upstream is configured -- push_refspec() is the required path"
        )

    monkeypatch.setattr(git_native, "push", _bare_push_must_not_be_called)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert not outcome.failed
    assert not outcome.unconfirmed
    assert _remote_head_sha(repo, _REMOTE_BRANCH) == local_head


def test_push_refspec_targets_remote_and_upstream_branch_ref(tmp_path, monkeypatch):
    repo = _init_mismatched_upstream_repo(tmp_path)

    calls: list = []

    def _fake_push_refspec(cwd, remote_name, local_ref, remote_ref, **kw):
        calls.append((remote_name, local_ref, remote_ref))
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_native, "push_refspec", _fake_push_refspec)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert calls == [("origin", "HEAD", f"refs/heads/{_REMOTE_BRANCH}")]
