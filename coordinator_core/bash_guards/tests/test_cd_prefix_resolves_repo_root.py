"""Repo-root-resolving guards must follow a leading `cd <dir> &&` in the
EXECUTED command, not stay pinned to the guard process's own cwd or the
payload's session-level cwd.

Bug row: state/bug-backlog/2026-08-27-destructive-git-and-scope-guards-
resolve-295928a71726.yaml. Observed twice in one session driving a throwaway
fixture repo under a temp dir with `cd <fixture> && git ...`:

  1. `check_validate_commit` (Check 5) reported a staged-path scope warning
     naming a path from the UNRELATED repo the guard process happened to be
     sitting in -- the fixture repo never staged or referenced it.
  2. `check_destructive_git_revert` blocked a harmless `git checkout -q .`
     inside the clean fixture, listing the OTHER repo's uncommitted files as
     what it would discard.

Both read the wrong repo's git state because the command's own `cd <dir>`
prefix moved its actual working directory somewhere the guard's cwd
resolution never looked. Fixed via `_bt_leading_cd_prefix_cwd`, reusing
`_bt_blanket_add_dash_c_cwd`'s "resolve from the command text, fail open on
a miss" shape for the ONE gap named in the bug row: a leading `cd <dir> &&`,
narrower than full cross-segment cwd tracking (out of scope here, and named
as such in `_bt_leading_cd_prefix_cwd`'s own docstring).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _bt_leading_cd_prefix_cwd,
    check_destructive_git_revert,
    check_validate_commit,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        **no_console_creationflags(),
    )


def _make_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@t.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    with open(os.path.join(path, "tracked.txt"), "w", encoding="utf-8") as fh:
        fh.write("committed\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


class TestLeadingCdPrefixCwd:
    def test_no_leading_cd_returns_none(self):
        assert _bt_leading_cd_prefix_cwd("git commit -m x", "/somewhere") is None

    def test_absolute_leading_cd_resolves(self, tmp_path):
        target = str(tmp_path / "fixture")
        assert _bt_leading_cd_prefix_cwd(
            f"cd {target} && git checkout -q .", "/elsewhere"
        ) == os.path.normpath(target)

    def test_glob_target_stays_unresolved(self):
        assert _bt_leading_cd_prefix_cwd("cd fixture* && git status", "/x") is None

    def test_unexpanded_var_target_stays_unresolved(self):
        assert _bt_leading_cd_prefix_cwd("cd $FIXTURE && git status", "/x") is None


class TestCommitScopeFollowsLeadingCd:
    def test_clean_fixture_repo_produces_no_warning_from_the_outer_repo(
        self, tmp_path, monkeypatch
    ):
        outer = _make_repo(str(tmp_path / "outer"))
        with open(os.path.join(outer, "outer_only.txt"), "w", encoding="utf-8") as fh:
            fh.write("outer staged\n")
        _git("add", "outer_only.txt", cwd=outer)

        fixture = _make_repo(str(tmp_path / "fixture"))

        # Guard process cwd (and the payload's session-level cwd) sits in
        # the OUTER repo, which has a staged path -- the pre-fix bug read
        # that staged path against the fixture's `git commit`.
        monkeypatch.chdir(outer)
        cmd = f"cd {fixture} && git commit -q -m x"
        result = check_validate_commit(cmd, "s1", outer, payload={"cwd": outer})
        assert result is None, (
            "commit-scope check must resolve the fixture repo's own "
            f"(empty) staged set, not the outer repo's -- got {result!r}"
        )


class TestDestructiveRevertFollowsLeadingCd:
    def test_clean_fixture_checkout_not_blocked_by_outer_repos_dirt(
        self, tmp_path, monkeypatch
    ):
        outer = _make_repo(str(tmp_path / "outer"))
        with open(os.path.join(outer, "dirty.txt"), "w", encoding="utf-8") as fh:
            fh.write("uncommitted in the OUTER repo\n")

        fixture = _make_repo(str(tmp_path / "fixture"))

        monkeypatch.chdir(outer)
        cmd = f"cd {fixture} && git checkout -q ."
        result = check_destructive_git_revert(cmd, "s1", {"cwd": outer})
        assert result is None, (
            "checkout in a clean fixture repo must not be blocked on the "
            f"OUTER repo's dirty state -- got {result!r}"
        )
