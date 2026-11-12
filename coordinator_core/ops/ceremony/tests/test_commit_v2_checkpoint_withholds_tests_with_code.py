"""A wave checkpoint through `ceremony.commit_v2` never lands a row's test while that row's code is
withheld; a row whose code is wholly the run's own lands whole."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cko-pair"
PLAN = "docs/plans/p.md"
SPINE = """# plan

## Tasks

```yaml plan-tasks
- id: R1
  title: row one
  disposition: open
  writes:
    - pkg/code.py
    - pkg/tests/test_code.py
- id: R2
  title: row two
  disposition: open
  writes:
    - pkg/other.py
    - pkg/tests/test_other.py
```
"""


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs/plans").mkdir(parents=True)
    (root / "pkg/tests").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / PLAN).write_bytes(SPINE.encode())
    for rel in ("pkg/code.py", "pkg/tests/test_code.py", "pkg/other.py", "pkg/tests/test_other.py"):
        (root / rel).write_bytes(b"v1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for rel in ("pkg/code.py", "pkg/tests/test_code.py", "pkg/other.py", "pkg/tests/test_other.py"):
        (root / rel).write_bytes(b"v2\n")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    # The run's write-time prints attribute a path to the run: record them through the production
    # recorder the way the executor's write hook does, never a stub of the reader.
    run_write_prints.record(
        root, "run1", ("pkg/code.py", "pkg/tests/test_code.py", "pkg/other.py", "pkg/tests/test_other.py"),
    )
    return root


def _checkpoint(repo: Path, rows: str, paths: list) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    count = len(rows.split(","))
    message = (
        f"checkpoint(wave 1): {count} rows — {rows}\n\n"
        f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"
    )
    return commit_v2._handler(
        {"paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True, "message": message},
        repo_root=repo / ".git",
    )


def _withhold(monkeypatch, withheld_path: str) -> None:
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == withheld_path else HunkSplit(own=list(hunks))
        ),
    )


def _landed(repo: Path) -> set:
    """Product paths committed since the seed (a checkpoint may add a separate process-path commit)."""
    seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
    return set(_git(["diff", "--name-only", f"{seed}..HEAD"], repo).split()) - {PLAN}


def test_withheld_code_takes_its_rows_test_with_it_and_other_row_lands(repo, monkeypatch):
    _withhold(monkeypatch, "pkg/code.py")
    paths = ["pkg/code.py", "pkg/tests/test_code.py", "pkg/other.py", "pkg/tests/test_other.py"]

    result = _checkpoint(repo, "R1, R2", paths)

    assert result["committed"] is True, result
    assert _landed(repo) == {"pkg/other.py", "pkg/tests/test_other.py"}
    assert result["checkpoint_withheld_paths"] == ["pkg/code.py", "pkg/tests/test_code.py"]
    assert (repo / "pkg/tests/test_code.py").read_bytes() == b"v2\n"


def test_withheld_test_alone_leaves_the_code_landing(repo, monkeypatch):
    _withhold(monkeypatch, "pkg/tests/test_code.py")

    result = _checkpoint(repo, "R1", ["pkg/code.py", "pkg/tests/test_code.py"])

    assert result["committed"] is True, result
    assert _landed(repo) == {"pkg/code.py"}
    assert result["checkpoint_withheld_paths"] == ["pkg/tests/test_code.py"]


def test_row_whose_code_is_own_lands_code_and_test(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )

    result = _checkpoint(repo, "R1", ["pkg/code.py", "pkg/tests/test_code.py"])

    assert result["committed"] is True, result
    assert _landed(repo) == {"pkg/code.py", "pkg/tests/test_code.py"}
    assert "checkpoint_withheld_paths" not in result


def test_deleted_test_of_withheld_code_stays_in_the_worktree(repo, monkeypatch):
    _withhold(monkeypatch, "pkg/code.py")
    (repo / "pkg/tests/test_code.py").unlink()

    result = _checkpoint(repo, "R1", ["pkg/code.py", "pkg/tests/test_code.py"])

    assert "pkg/tests/test_code.py" not in _landed(repo)
    assert "pkg/code.py" not in _landed(repo)
    assert "pkg/tests/test_code.py" in result.get("checkpoint_withheld_paths", [])
