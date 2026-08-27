"""
coordinator_core.ops.ceremony.tests.fixtures.real_git

These fixtures spawn real git. They exist for the commit-mechanism
selector's tests only. The suites in coordinator_core/ops/ceremony/tests/
deliberately mock git -- importing this module into a mocked suite is a
review finding, not a convenience.

Index/worktree divergence -- the thing the commit-mechanism selector has to
tell apart -- cannot be exhibited by a mocked git: a mock has no index, so
there is nothing for a real "staged vs. worktree" state to diverge FROM.
This module inits a real, throwaway repo per call and drives real
``git add``/``git commit`` against it so the divergence states below are
the real ones a selector will see on a live shared tree, not a stand-in.

Deliberately a plain importable module, not a conftest.py: a conftest
fixture is auto-discovered and ambient to every test in this directory,
including test_wsc_commit.py, test_wsc_resolve.py and test_wsc_tail_parity.py
(300+ tests total) which carry their own "Patched in tests -- do NOT call
real git in test assertions" instruction. An explicit
``from .fixtures.real_git import real_git_repo`` makes the mock-vs-real
crossing visible in the diff and in review, instead of silently handing
real git to every test in the package.

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C2.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a real git command against ``cwd``, raising on nonzero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def real_git_repo(tmp_path: Path) -> Path:
    """Init a real, seeded git repo under ``tmp_path`` and return its root.

    The seed commit exists so every state helper below has a committed
    baseline to diverge from -- an empty repo has no HEAD, and staging
    against a repo with no HEAD is not the shape a live coordinator working
    tree ever presents.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "real-git-fixture@example.invalid"], root)
    _git(["config", "user.name", "real-git-fixture"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def make_agree_path(root: Path, rel: str, content: str) -> None:
    """Write + stage ``rel`` so index and worktree AGREE for that path.

    This is the horn where ``git commit -- <paths>`` (reading the worktree)
    and committing the staged index produce the identical result -- the
    safe case the selector should recognize and fast-path.
    """
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "--", rel], root)


def make_diverged_path(root: Path, rel: str, staged_content: str, worktree_content: str) -> None:
    """Stage ``rel`` at ``staged_content``, then further edit the worktree
    to ``worktree_content`` -- a DIVERGED path (partial-hunk staging, or a
    stage-then-keep-editing sequence). ``staged_content`` and
    ``worktree_content`` must differ for the divergence to be real; the
    caller is responsible for passing distinct values.

    ``git commit -- rel`` here would silently commit ``worktree_content``
    and discard the deliberately staged ``staged_content`` hunk -- this is
    the claude-klabauter 506748a0 incident shape.
    """
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(staged_content, encoding="utf-8")
    _git(["add", "--", rel], root)
    p.write_text(worktree_content, encoding="utf-8")


def make_peer_staged_path(root: Path, rel: str, content: str) -> None:
    """Stage ``rel`` as a stand-in for a concurrent peer session's
    in-flight staged work -- a path the caller's own scope never names.

    A pathspec-less ``git commit`` sweeps this into the caller's commit
    alongside their own paths -- this is the DoE-claude 726925b2 incident
    shape.
    """
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "--", rel], root)
