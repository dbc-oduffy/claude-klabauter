"""
coordinator_core.ops.ceremony.tests.test_commit_pipeline_push_spawns

Regression pin for C2b (docs/dispatch-briefs/2026-08-25-push-re-homes-onto-
the-cadence-surfaces/C2b.md): `push_with_retry`'s LOCAL half -- the no-
remote check, the current-branch/upstream-ref resolution, the pre-push
upstream sha, and the post-push HEAD sha -- must cost ZERO `git` spawns.
Only two spawns remain by design: `git push` itself (the network leg, out
of this chunk's scope) and `git rev-list --count` (the pushed-range REPORT,
deliberately deferred until a push actually lands -- see `push_with_retry`'s
docstring and `_resolve_pushed_range`).

Before C2b this same scenario cost SIX spawns (measured in the brief:
`git remote`, `git rev-parse --abbrev-ref HEAD`, `git rev-parse
--symbolic-full-name @{u}`, `git rev-parse @{u}`, `git rev-parse HEAD`,
`git rev-list --count @{u}..HEAD}` -- 66.4ms/6 spawns to read three refs
and ask whether a remote exists). This test asserts the SPAWN COUNT
directly (not just the outcome) so a later regression that reintroduces a
spawn on this path fails loudly here rather than silently costing 10ms
forever.

Negative-spec: does NOT assert on `git fetch`/`git rebase`/retry-loop
spawns -- the reject -> fetch -> rebase -> re-push recovery path is a
genuine network/history operation, out of this chunk's "local half" scope
(see the C2b brief's "IF A SPAWN IS GENUINELY UNAVOIDABLE" note, which is
about THAT path, not this one).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Sequence

import pytest

import coordinator_core.ops.ceremony.git_native as git_native_mod
from coordinator_core.ops.ceremony.push import push_with_retry
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args: Sequence[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_repo_with_pushed_upstream(tmp_path: Path) -> Path:
    """A `work/*`-branch repo with `origin` already configured and an
    upstream tracking ref already set (mirrors what any live `push_with_
    retry` caller's repo looks like -- upstream is established by an
    EARLIER push, never by this call).
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/c2b-push-spawns"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "-q", "-u", "origin", "work/c2b-push-spawns"], repo)
    return repo


class _GitSpy:
    """Wraps `git_native._git`, recording every argv this test's call to
    `push_with_retry` actually spawns -- a true spawn count, not a mock
    standing in for one, since every `git_native` wrapper `push_with_retry`
    can reach routes through this single choke point (module docstring).
    """

    def __init__(self, real_git):
        self._real_git = real_git
        self.calls: List[List[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return self._real_git(args, **kwargs)


def test_push_with_retry_local_half_costs_zero_spawns(tmp_path, monkeypatch):
    repo = _init_repo_with_pushed_upstream(tmp_path)

    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _git(["add", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    spy = _GitSpy(git_native_mod._git)
    monkeypatch.setattr(git_native_mod, "_git", spy)

    outcome = push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert outcome.pushed_count == 1

    # Exactly two spawns: the push itself, and the pushed-range REPORT
    # (rev-list --count), spent only because this push actually landed.
    assert len(spy.calls) == 2, spy.calls
    assert spy.calls[0][0] == "push"
    assert spy.calls[1][:2] == ["rev-list", "--count"]

    # No local-half spawn survived: no `git remote`, no `git rev-parse`
    # (branch/upstream/HEAD) anywhere in what was actually spawned.
    spawned_subcommands = {call[0] for call in spy.calls}
    assert "remote" not in spawned_subcommands
    assert "rev-parse" not in spawned_subcommands


def test_push_with_retry_no_remote_skip_costs_zero_spawns(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/c2b-no-remote"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    spy = _GitSpy(git_native_mod._git)
    monkeypatch.setattr(git_native_mod, "_git", spy)

    outcome = push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:no-remote"]
    assert spy.calls == []
