"""Regression tests for `apply._scoped_commit` and the mutating-verb path of
`pickup_assemble._run_git`.

Root cause guarded here: the W0-2 in-process git read-model refactor
(commit `59d2963e`, "in-process .git read-model, 11/12 git spawns removed")
routed every `_run_git` verb except `status`/`diff` through the read-only
`_dispatch_git_readmodel`. Its spawn-site sweep was scoped to `__init__.py`'s
read-only callers and missed `apply._scoped_commit`, a cross-module consumer
that calls `_run_git(["add", ...])` and `_run_git(["commit", ...])` — MUTATING
verbs a read-model can never serve. The read-model raised `_GitReadModelError`,
which the choke-point guard swallowed into `CompletedProcess(returncode=1,
stdout="", stderr="")`, so `_scoped_commit` raised
`RuntimeError("git add <path> failed:")` with an EMPTY stderr — a valid `git
add` failing silently.

These tests build throwaway `git init` fixtures with real `git` and exercise
`_scoped_commit` the way `apply()` does. They FAIL (RuntimeError, empty stderr)
against the pre-fix module and pass once `add`/`commit` spawn real git and the
read-model resolves `rev-parse HEAD`.

Spec backlink: DoE-claude DoE-claude:pln-canonical-resolution-engine-6eea37
task W0-2 (the refactor whose caller sweep this test closes).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from coordinator_core import pickup_assemble as pa  # noqa: E402
from coordinator_core.pickup_assemble import apply as pa_apply  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    **no_console_creationflags())


@pytest.fixture()
def repo() -> Path:
    """A throwaway git repo with one initial commit, so HEAD exists."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        _git(["init", "-q"], root)
        _git(["config", "user.email", "t@example.invalid"], root)
        _git(["config", "user.name", "Test"], root)
        (root / "seed.txt").write_text("seed\n")
        _git(["add", "--", "seed.txt"], root)
        _git(["commit", "-q", "-m", "seed"], root)
        yield root


def test_run_git_add_actually_stages(repo: Path):
    """`_run_git(["add", ...])` must spawn real git and stage the path —
    not degrade to the read-model's empty-stderr non-zero fallback."""
    (repo / "artifact.md").write_text("body\n")
    proc = pa._run_git(["add", "--", str(repo / "artifact.md")], repo)
    assert proc.returncode == 0, f"add degraded: rc={proc.returncode} stderr={proc.stderr!r}"
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, timeout=30,
    **no_console_creationflags())
    assert "artifact.md" in staged.stdout


def test_run_git_rev_parse_head_returns_sha(repo: Path):
    """`rev-parse HEAD` must resolve to the current commit SHA (read-model or
    spawn) — a returncode-1 here silently strands `_scoped_commit`'s SHA."""
    proc = pa._run_git(["rev-parse", "HEAD"], repo)
    assert proc.returncode == 0, f"rev-parse HEAD degraded: stderr={proc.stderr!r}"
    real = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30,
    **no_console_creationflags())
    assert proc.stdout.strip() == real.stdout.strip()


def test_scoped_commit_commits_new_artifact(repo: Path):
    """The break-class repro: `_scoped_commit` on a freshly-written artifact
    must stage+commit it and return the new SHA — not raise on a silent
    empty-stderr `git add` failure."""
    rel = "state/handoffs/2026-07-23_example-handoff.md"
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# handoff\n")

    sha = pa_apply._scoped_commit(repo, rel, "handoff", "example-handoff.md", ["d1"])

    assert sha is not None and len(sha.strip()) == 40, f"expected a commit SHA, got {sha!r}"
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30,
    **no_console_creationflags())
    assert sha.strip() == head.stdout.strip()
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", rel],
        capture_output=True, text=True, timeout=30,
    **no_console_creationflags())
    assert rel in tracked.stdout


def test_scoped_commit_scopes_to_the_one_path(repo: Path):
    """A pre-existing staged peer file must survive `_scoped_commit`
    uncommitted — the pathspec must not widen (AC10)."""
    peer = repo / "peer.txt"
    peer.write_text("peer\n")
    _git(["add", "--", "peer.txt"], repo)

    rel = "artifact.md"
    (repo / rel).write_text("body\n")
    sha = pa_apply._scoped_commit(repo, rel, "handoff", "artifact.md", ["d1"])
    assert sha is not None

    # peer.txt was staged but NOT part of this commit's pathspec — still staged.
    still_staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, timeout=30,
    **no_console_creationflags())
    assert "peer.txt" in still_staged.stdout
    assert "artifact.md" not in still_staged.stdout
