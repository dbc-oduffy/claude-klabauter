"""
coordinator_core.pickup_assemble.tests.test_read_only_invariant — pins the
module docstring's READ-ONLY guarantee (AC2b/AC3) that, until now, was
asserted only in prose.

Spec backlink: state/audits/2026-08-13-pickup-negative-spec-adjudication.md § 7
Baton: state/handoffs/2026-08-13-reconcile-evidence-at-pickup-cadence.md

The existing `test_brief_mutates_nothing_on_disk` (coordinator_core/
test_pickup_assemble.py) only checks `git status --porcelain`, which is a
worktree-only view — it cannot see a write landing under `.git/` itself
(session/claim/history state, say) because untracked writes inside the git
common dir never show up in porcelain output at all. This file closes that
gap: it snapshots BOTH the worktree and the git common dir before and after
a `brief()` call and asserts neither changed. The `apply` path is out of
scope — it writes by design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa

# Real-git spawn in the fixture (git init/add/commit) is load-bearing —
# `brief()`'s classifiers read actual git-tracked repo state. Declares the
# spawn per the ratchet's Rule 2(b) marker escape (coordinator_core/tests/
# test_no_new_spawning_tests.py) rather than an allowlist entry.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    config_file = anchor / ".gitconfig-empty"
    if not config_file.exists():
        config_file.write_text("", encoding="utf-8")
    env["GIT_CONFIG_GLOBAL"] = str(config_file)
    env["GIT_CONFIG_SYSTEM"] = str(config_file)
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _snapshot(root: Path) -> set[tuple[str, int, int]]:
    """(relative-path, mtime_ns, size) for every regular file under `root`.

    The ONE filtered entry is CPython's own import-machinery bytecode cache:
    a file whose immediate parent directory is literally named
    `__pycache__` AND whose suffix is `.pyc` — the exact shape CPython
    writes when this test process imports `coordinator_core`, not anything
    `brief()` does. Both conditions are required together so the filter
    cannot be widened by accident: a stray `.pyc` written outside a
    `__pycache__` directory, or any non-`.pyc` file that happens to live
    under one, is NOT excluded and will still be caught as churn.

    Nothing else is filtered, deliberately. `brief()` DOES spawn git
    (`_run_git`, reached from `_branch_age_days` / `_git_log_oneline`), so
    read-side housekeeping churn under `.git/` is not impossible a priori —
    it simply does not occur for the read-only plumbing commands `brief()
    ` issues. If it ever appears, that is the finding this test exists to
    surface, not noise to launder away: widening this filter to restore
    green would delete the invariant the file is here to pin.
    """
    snap: set[tuple[str, int, int]] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.parent.name == "__pycache__" and path.suffix == ".pyc":
            continue
        st = path.stat()
        snap.add((str(path.relative_to(root)), st.st_mtime_ns, st.st_size))
    return snap


class TestBriefReadOnlyAcrossWorktreeAndGitCommonDir:
    def test_brief_writes_nothing_under_worktree_or_git_dir(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        git_dir = repo / ".git"
        # `_snapshot(repo)` already spans `.git/` — its paths are relative to
        # `repo`, so it is NOT set-subtractable against a `git_dir`-relative
        # snapshot. Both roots are asserted, the second at its own path base
        # so a failure names the offending file relative to the git dir.
        tree_before = _snapshot(repo)
        git_before = _snapshot(git_dir)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        tree_after = _snapshot(repo)
        git_after = _snapshot(git_dir)

        assert result.exit_code == pa.EXIT_OK
        assert tree_before == tree_after
        assert git_before == git_after
