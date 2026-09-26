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
    _bt_cd_chain_cwd_before,
    _bt_leading_cd_prefix_cwd,
    check_destructive_git_orphan,
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

        monkeypatch.chdir(outer)
        cmd = f"cd {fixture} && git commit -q -m x"
        result = check_validate_commit(cmd, "s1", outer, payload={"cwd": outer})
        assert result is None, (
            "commit-scope check must resolve the fixture repo's own "
            f"(empty) staged set, not the outer repo's -- got {result!r}"
        )


class TestDestructiveGitOrphanRelativeCDirResolvesAgainstPayloadCwd:
    """Bug row: state/bug-backlog/2026-09-12-check-1-skips-verification-on-a-
    relative-c-dir-91b4c7de20a3.yaml. A relative `git -C <dir>` (`-C reset`
    or `-C ./reset`) used to reach `_memo_run_git`/`_run_git` unresolved,
    where it becomes the probe subprocess's `cwd=` -- resolving against the
    GUARD PROCESS's own directory, not the payload's, so the probes fail
    unverifiable and CHECK 1 reads that as nothing to deny. The identical
    command naming the same directory ABSOLUTELY was verified correctly the
    whole time; the fix makes the two spellings agree.
    """

    @pytest.fixture()
    def _outer_and_nested_ahead_repo(self, tmp_path):
        outer = _make_repo(str(tmp_path / "outer"))
        _git("branch", "-M", "main", cwd=outer)

        nested = _make_repo(str(tmp_path / "outer" / "reset"))
        _git("branch", "-M", "main", cwd=nested)
        _git("checkout", "-qb", "work", cwd=nested)
        with open(os.path.join(nested, "ahead.txt"), "w", encoding="utf-8") as fh:
            fh.write("commit only reachable from work\n")
        _git("add", "ahead.txt", cwd=nested)
        _git("commit", "-qm", "ahead of main", cwd=nested)
        return outer, nested

    @pytest.mark.parametrize("c_dir_form", ["reset", "./reset"])
    def test_relative_c_dir_denies_same_as_absolute(
        self, _outer_and_nested_ahead_repo, monkeypatch, c_dir_form
    ):
        outer, nested = _outer_and_nested_ahead_repo

        monkeypatch.chdir(os.path.dirname(outer))

        abs_result = check_destructive_git_orphan(
            f"git -C {nested} reset --hard main", "s1", payload={"cwd": outer}
        )
        assert abs_result is not None
        assert abs_result["hookSpecificOutput"]["permissionDecision"] == "deny"

        rel_result = check_destructive_git_orphan(
            f"git -C {c_dir_form} reset --hard main", "s1", payload={"cwd": outer}
        )
        assert rel_result is not None, (
            "relative `-C %s` must resolve against the payload cwd and deny "
            "identically to the absolute form -- got an allow instead" % c_dir_form
        )
        assert rel_result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_relative_c_dir_falls_back_to_git_root_when_payload_cwd_absent(
        self, _outer_and_nested_ahead_repo, monkeypatch
    ):
        outer, nested = _outer_and_nested_ahead_repo
        monkeypatch.chdir(os.path.dirname(outer))

        result = check_destructive_git_orphan(
            "git -C reset reset --hard main", "s1", payload={}, git_root=outer
        )
        assert result is not None, (
            "with no payload cwd, the base must fall back to git_root -- "
            "got an allow instead"
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_relative_c_dir_without_any_base_stays_unresolved(self):
        from coordinator_core.bash_guards.dispatch_checks import _orphan_c_cwd

        assert _orphan_c_cwd("reset", None) == "reset"


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


class TestBtCdChainCwdBefore:

    def test_no_prior_cd_segment_returns_none(self):
        segments = ["rm -rf x", "git reset --hard main"]
        assert _bt_cd_chain_cwd_before(segments, 1, "/base") is None

    def test_single_leading_cd_resolves(self, tmp_path):
        target = str(tmp_path / "fixture")
        segments = [f"cd {target} ", " git reset --hard main"]
        assert _bt_cd_chain_cwd_before(segments, 1, "/elsewhere") == os.path.normpath(
            target
        )

    def test_chain_with_intervening_non_cd_command_resolves_the_inner_cd(
        self, tmp_path
    ):
        outer = str(tmp_path / "scratch")
        segments = [
            f"cd {outer} ",
            " rm -rf sub",
            " mkdir sub",
            " cd sub",
            " git reset --hard main",
        ]
        assert _bt_cd_chain_cwd_before(
            segments, 4, "/elsewhere"
        ) == os.path.normpath(os.path.join(outer, "sub"))

    def test_unresolvable_outer_cd_abandons_the_whole_chain(self, tmp_path):
        segments = [
            "cd $TEMP ",
            " cd sub",
            " git reset --hard main",
        ]
        assert _bt_cd_chain_cwd_before(segments, 2, "/elsewhere") is None

    def test_glob_target_abandons_the_whole_chain(self, tmp_path):
        segments = ["cd fixture* ", " git status"]
        assert _bt_cd_chain_cwd_before(segments, 1, "/elsewhere") is None


class TestDestructiveGitOrphanFollowsCdChain:
    """Row: state/bug-backlog/2026-09-11-destructive-git-orphan-judges-a-
    reset-ha-096ab8220a8a.yaml. The guard's own leading-`cd`-only fallback
    misses an inner `cd` that follows a non-`cd` command (`rm`, `mkdir`), so
    it fell through to the guard process's own cwd -- ALLOWING a reset that
    orphans work in the real target repo, and DENYING a harmless one, purely
    on the unrelated state of whatever tree the guard process happens to sit
    in."""

    def test_orphaning_reset_in_target_repo_is_not_judged_against_engine_tree(
        self, tmp_path, monkeypatch
    ):
        engine = _make_repo(str(tmp_path / "engine"))
        _git("branch", "-M", "main", cwd=engine)
        _git("checkout", "-qb", "candidate", cwd=engine)
        with open(os.path.join(engine, "ahead.txt"), "w", encoding="utf-8") as fh:
            fh.write("commit only reachable from candidate\n")
        _git("add", "ahead.txt", cwd=engine)
        _git("commit", "-qm", "ahead of main", cwd=engine)

        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch, exist_ok=True)
        sub = _make_repo(os.path.join(scratch, "sub"))
        _git("branch", "-M", "main", cwd=sub)

        monkeypatch.chdir(engine)
        cmd = (
            f"cd {scratch} && rm -rf other && mkdir other && cd sub && "
            "git reset --hard main"
        )
        result = check_destructive_git_orphan(cmd, "s1", payload={"cwd": engine})
        assert result is None, (
            "reset that is a no-op in the scratch repo's own history must "
            f"not be denied on the engine repo's ahead branch -- got {result!r}"
        )

    def test_orphaning_reset_in_target_repo_is_still_denied_when_engine_tree_is_clean(
        self, tmp_path, monkeypatch
    ):
        engine = _make_repo(str(tmp_path / "engine"))

        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch, exist_ok=True)
        sub = _make_repo(os.path.join(scratch, "sub"))
        _git("branch", "-M", "main", cwd=sub)
        _git("checkout", "-qb", "candidate", cwd=sub)
        with open(os.path.join(sub, "ahead.txt"), "w", encoding="utf-8") as fh:
            fh.write("commit only reachable from candidate\n")
        _git("add", "ahead.txt", cwd=sub)
        _git("commit", "-qm", "ahead of main", cwd=sub)

        monkeypatch.chdir(engine)
        cmd = (
            f"cd {scratch} && rm -rf other && mkdir other && cd sub && "
            "git reset --hard main"
        )
        result = check_destructive_git_orphan(cmd, "s1", payload={"cwd": engine})
        assert result is not None, (
            "reset that would drop the scratch repo's own ahead commit must "
            "be denied even though the engine repo the guard would otherwise "
            "default to has nothing ahead"
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
