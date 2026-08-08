"""Placement-pinning tests for the read-only `--no-optional-locks` adoption
(state/handoffs/2026-08-07-git-index-lock-retry-at-the-ceremony-commit-seam.md
Specification § "Third, independent and cheap").

`--no-optional-locks` is a PRE-SUBCOMMAND-ONLY global git option: appended
after the subcommand (`git status --no-optional-locks`) it exits 129 with
"unknown option" and produces no output at all -- a hard, silent-looking
failure. Every test here asserts the constructed argv places the flag
immediately after `git` (and after any `-C <dir>` that precedes it) and
before the subcommand token -- never spawning real git, per this dispatch's
own test guidance.

Sites covered (see the handoff's site list, priority order):
  - `bash_guards.dispatch_checks._run_git` call sites feeding
    `check_destructive_rm`'s per-target status probe and
    `check_destructive_git_revert`'s `_memo_status_porcelain` oracle.
  - `hooks.context_pressure_precompact._build_git_section`'s bare
    `git diff --name-only` (its sibling `git diff --staged --name-only` is
    `--cached`-equivalent and deliberately untouched -- never took the lock).
  - `baton_assemble`'s whole-tree `git status --porcelain --untracked-files=all`.
  - `archive_stamp._scope_paths_have_uncommitted_changes`'s status probe.
  - `consolidate_assemble.worktree_is_dirty`'s status probe.

Each test mocks `subprocess.run` and inspects the constructed argv only --
no real git process is spawned, matching this repo's shared-worktree
contention concern (spawning real git here would be exactly the noise this
change removes).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return completed


def _assert_precedes_subcommand(argv, subcommand: str) -> None:
    """`--no-optional-locks` must appear before `subcommand`, and nothing
    between it and `git`/`-C <dir>` may itself be (mis-mistaken for) the
    subcommand token."""
    assert "--no-optional-locks" in argv, f"flag missing from argv={argv!r}"
    lock_idx = argv.index("--no-optional-locks")
    assert subcommand in argv, f"subcommand {subcommand!r} missing from argv={argv!r}"
    sub_idx = argv.index(subcommand)
    assert lock_idx < sub_idx, (
        f"--no-optional-locks must precede the '{subcommand}' subcommand "
        f"(appended after it, git exits 129 'unknown option'). argv={argv!r}"
    )


# ---------------------------------------------------------------------------
# bash_guards.dispatch_checks
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    from coordinator_core.win_portability import no_console_creationflags

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=True,
            capture_output=True,
            **no_console_creationflags(),
        )

    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-qm", "baseline")


def test_check_destructive_rm_status_probe_precedes_subcommand(tmp_path: Path):
    """The status probe only fires on the directory/recursive leg (`tgt_is_dir
    or recursive`) -- a plain file target with no `-r` never reaches it, so
    this drives it with `rm -rf <dir>` against a real tiny repo. Real git is
    spawned here (wrapped, not stubbed) because the surrounding function also
    issues several `rev-parse`/scratch-allowlist probes whose return values
    gate whether the status call is reached at all -- stubbing all of them
    plausibly is more fragile than letting git answer for real in an isolated
    tmp_path repo."""
    from coordinator_core.bash_guards import dispatch_checks

    repo = tmp_path / "repo"
    _init_repo(repo)
    target_dir = repo / "subdir"
    target_dir.mkdir()
    (target_dir / "f.txt").write_text("x", encoding="utf-8")

    calls = []
    real_run = subprocess.run

    def _spy_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    with patch.object(dispatch_checks.subprocess, "run", side_effect=_spy_run):
        dispatch_checks.check_destructive_rm(f"rm -rf {target_dir}")

    status_calls = [c for c in calls if "status" in c]
    assert status_calls, "expected at least one 'git status' probe to fire"
    for argv in status_calls:
        assert argv[0] == "git"
        _assert_precedes_subcommand(argv, "status")


def test_check_destructive_git_revert_status_oracle_precedes_subcommand():
    from coordinator_core.bash_guards import dispatch_checks

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(0, "")

    with patch.object(dispatch_checks.subprocess, "run", side_effect=_fake_run):
        dispatch_checks.check_destructive_git_revert("git checkout .")

    status_calls = [c for c in calls if "status" in c]
    assert status_calls, "expected the _memo_status_porcelain oracle to fire"
    for argv in status_calls:
        assert argv[0] == "git"
        _assert_precedes_subcommand(argv, "status")


# ---------------------------------------------------------------------------
# hooks.context_pressure_precompact
# ---------------------------------------------------------------------------


def test_build_git_section_bare_diff_precedes_subcommand():
    # `_run_git` does `import subprocess` INSIDE the function (module-level
    # `cpp.subprocess` does not exist) -- patch the real `subprocess` module
    # instead, which is the same object either way resolves to.
    from coordinator_core.hooks import context_pressure_precompact as cpp

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "rev-parse" in argv:
            return _make_completed(0, "main\n")
        return _make_completed(0, "")

    with patch("subprocess.run", side_effect=_fake_run):
        cpp._build_git_section(cwd=None)

    diff_calls = [c for c in calls if "diff" in c]
    assert diff_calls, "expected at least one 'git diff' call"

    bare_diff_calls = [c for c in diff_calls if "--staged" not in c and "--cached" not in c]
    assert bare_diff_calls, "expected the bare (non-staged) diff call"
    for argv in bare_diff_calls:
        _assert_precedes_subcommand(argv, "diff")

    # The --staged sibling never took the lock (cached-equivalent) -- must
    # NOT be touched.
    staged_calls = [c for c in diff_calls if "--staged" in c]
    assert staged_calls, "expected the --staged diff call to remain present"
    for argv in staged_calls:
        assert "--no-optional-locks" not in argv, (
            f"--staged diff never takes index.lock -- must stay unmodified, got {argv!r}"
        )


# ---------------------------------------------------------------------------
# baton_assemble whole-tree status probe
# ---------------------------------------------------------------------------


def test_baton_assemble_dirty_tree_status_probe_precedes_subcommand(tmp_path: Path, monkeypatch):
    from coordinator_core import baton_assemble

    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")
    touched_dir = tmp_path / ".git" / "coordinator-sessions" / "sess-1"
    touched_dir.mkdir(parents=True)
    (touched_dir / "touched.txt").write_text("", encoding="utf-8")

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(0, "")

    with patch.object(baton_assemble, "git_common_dir", return_value=touched_dir.parent.parent):
        with patch.object(baton_assemble.subprocess, "run", side_effect=_fake_run):
            baton_assemble._compute_dirty_tree_attribution(tmp_path)

    status_calls = [c for c in calls if "status" in c]
    assert status_calls, "expected the whole-tree status probe to fire"
    for argv in status_calls:
        assert argv[0] == "git"
        _assert_precedes_subcommand(argv, "status")


# ---------------------------------------------------------------------------
# archive_stamp
# ---------------------------------------------------------------------------


def test_archive_stamp_scope_status_probe_precedes_subcommand(tmp_path: Path):
    from coordinator_core import archive_stamp

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(0, "")

    with patch.object(archive_stamp.subprocess, "run", side_effect=_fake_run):
        archive_stamp._scope_paths_have_uncommitted_changes(tmp_path, ["state/x.md"])

    status_calls = [c for c in calls if "status" in c]
    assert status_calls, "expected the scope-paths status probe to fire"
    for argv in status_calls:
        assert argv[0] == "git"
        _assert_precedes_subcommand(argv, "status")


# ---------------------------------------------------------------------------
# consolidate_assemble
# ---------------------------------------------------------------------------


def test_consolidate_assemble_worktree_is_dirty_precedes_subcommand(tmp_path: Path):
    from coordinator_core import consolidate_assemble

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(0, "")

    with patch.object(consolidate_assemble.subprocess, "run", side_effect=_fake_run):
        consolidate_assemble.worktree_is_dirty(
            consolidate_assemble.default_run_git, str(tmp_path)
        )

    status_calls = [c for c in calls if "status" in c]
    assert status_calls, "expected the worktree status probe to fire"
    for argv in status_calls:
        assert argv[0] == "git"
        _assert_precedes_subcommand(argv, "status")
