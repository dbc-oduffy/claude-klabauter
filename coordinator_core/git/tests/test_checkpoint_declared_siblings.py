"""A checkpoint row whose declared writes straddle the commit and a withheld path carries: `ceremony.commit_v2`
lands the own paths, withholds the foreign-hunk path with a reason, and leaves the row unstamped."""

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

PLAN = "docs/plans/p.md"
SELF = "cko-siblings"
SPINE = """# plan

## Tasks

```yaml plan-tasks
- id: C1
  title: two directories
  disposition: open
  writes:
    - schema/a.json
    - tool/b.py
    - tool/unchanged.py
- id: C2
  title: other row
  disposition: open
  writes:
    - other/c.py
```
"""
MESSAGE = f"checkpoint(wave 1): 1 rows — C1\n\nCheckpoint-Plan: {PLAN}\n"


@pytest.fixture
def plan_root(tmp_path):
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / PLAN).write_bytes(SPINE.encode())
    return tmp_path


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(plan_root, monkeypatch):
    root = plan_root
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "schema").mkdir()
    (root / "tool").mkdir()
    rels = ("schema/a.json", "tool/b.py")
    for rel in rels:
        (root / rel).write_bytes(b"v1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for rel in rels:
        (root / rel).write_bytes(b"v2\n")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    run_write_prints.record(root, "run1", rels)
    return root


def _checkpoint(repo: Path) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return commit_v2._handler(
        {
            "paths": ["schema/a.json", "tool/b.py"], "skip_missing": True,
            "message": f"{MESSAGE}Checkpoint-Base: {base}\n",
        },
        repo_root=repo / ".git",
    )


@pytest.mark.spawns_process
def test_commit_carries_a_row_split_by_a_foreign_hunk(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == "tool/b.py" else HunkSplit(own=list(hunks))
        ),
    )

    result = _checkpoint(repo)

    assert result["committed"] is True, result
    assert result["checkpoint_withheld_paths"] == ["tool/b.py"], result
    assert result["checkpoint_withheld_reasons"]["tool/b.py"], result
    assert _git(["show", "--name-only", "--format=", "HEAD"], repo).split() == ["schema/a.json"]
    message = _git(["log", "-1", "--format=%B"], repo)
    assert message.splitlines()[0] == "checkpoint(wave 1): 0 rows — (none)", message
    assert "Checkpoint-Withheld-Rows: C1" in message, message


@pytest.mark.spawns_process
def test_commit_lands_every_declared_write_when_all_are_own(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )

    result = _checkpoint(repo)

    assert result["committed"] is True, result
    assert "checkpoint_withheld_paths" not in result
