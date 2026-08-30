"""Tests for `substrate_migrate`'s git-tracking guard, against REAL git repos.

Split out of the sibling `test_substrate_migrate.py` on 2026-08-23. Every test
here reaches `git` through the module-level `_git_repo_tracking` helper, where a
per-function `pytest.mark` is inert -- pytest applies marks only to what it
collects -- so the spawn-ratchet's only accepted remediation is a module-level
`pytestmark` that tiers the whole file onto cadence. In the combined file that
meant exiling 40 spawn-free tests (0.6s) to move 3 spawning ones (0.9s).

Faking the predicate was the recorded recommendation and is REJECTED here.
`_tracked_file_count`'s entire job is to answer a question about git's index; a
stubbed answer pins the guard's message while leaving the query it wraps
unverified, which is what `_git_repo_tracking`'s own docstring already says. One
of the three tests cannot be faked even in principle --
`test_tracked_legacy_dir_guard_mutates_no_git_state` asserts that real git state
is unchanged, and there is no git state to leave alone in a fake.

The spawn-free half of the predicate's coverage
(`test_tracked_file_count_is_none_when_claude_base_is_not_a_repo`, the no-repo
short-circuit) stays in the sibling file and on the per-commit path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install import substrate_migrate as sm
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _mock_posix(monkeypatch):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


# ---------------------------------------------------------------------------
# structural guard — a git-TRACKED legacy machine-local can never converge
# (2026-08-22 install dogfood: `DIVERGENT FILE` on machine-local/.gitignore,
# from a `~/.claude` meta-repo that tracked the whole directory)
# ---------------------------------------------------------------------------


def _git_repo_tracking(claude_base: Path, *, tracked: list[str]) -> None:
    """Make `claude_base` a real git work tree with `tracked` committed under
    it. A real repo, not a stub: the guard's whole job is to answer a question
    about git's index, and a stubbed answer would pin the message while
    leaving the query itself unverified."""
    import subprocess

    claude_base.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(claude_base), *a],
        check=True,
        capture_output=True,
        env=env,
        **no_console_creationflags(),
    )
    run("init", "-q")
    for rel in tracked:
        target = claude_base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    run("add", "-A")
    run("commit", "-qm", "seed")


def test_tracked_legacy_dir_fails_with_the_structural_cause_not_a_file_diff(
    tmp_path, monkeypatch, capsys
):
    """THE REPORTED FAILURE. The operator was told one file diverged; the
    actual cause was that the whole legacy directory is tracked, so every
    re-run fails identically no matter how many files they reconcile by
    hand. The message must name the tracked directory, the repo tracking it,
    and why the migration cannot reach its terminal state."""
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    _git_repo_tracking(
        claude_base, tracked=["machine-local/.gitignore", "machine-local/registry.local.toml"]
    )
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot converge" in err
    assert str(claude_base / "machine-local") in err
    assert "2 tracked files" in err
    assert "DIVERGENT FILE" not in err, "the per-file symptom must not stand in for the cause"


def test_tracked_legacy_dir_guard_mutates_no_git_state(tmp_path, monkeypatch):
    """Explicitly out of scope, and pinned so it stays that way: the guard
    reads git and touches nothing. The repo it is reading is an operator's
    meta-repo synced to their other machines."""
    import subprocess

    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    _git_repo_tracking(claude_base, tracked=["machine-local/.gitignore"])
    before = subprocess.run(
        ["git", "-C", str(claude_base), "status", "--porcelain=v1"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    head_before = subprocess.run(
        ["git", "-C", str(claude_base), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout

    sm.migrate_substrate_to_settings_home(claude_base, tmp_path / "settings-home", check_only=False)

    after = subprocess.run(
        ["git", "-C", str(claude_base), "status", "--porcelain=v1"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    head_after = subprocess.run(
        ["git", "-C", str(claude_base), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert (before, head_before) == (after, head_after)
    assert (claude_base / "machine-local" / ".gitignore").is_file()


def test_untracked_legacy_dir_inside_a_git_repo_still_migrates(tmp_path, monkeypatch):
    """The resolved shape of the same box: `~/.claude` is still a git repo,
    but `machine-local/` is no longer tracked. The guard must key on TRACKING,
    not on the presence of a repo, or it would block every operator whose
    settings home is version-controlled at all."""
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    _git_repo_tracking(claude_base, tracked=["settings.json"])
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("repos.foo = 1")
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    assert ml.is_symlink()


