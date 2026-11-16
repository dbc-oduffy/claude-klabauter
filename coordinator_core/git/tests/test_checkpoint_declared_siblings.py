"""`declared_sibling_split` names a checkpoint row whose declared writes straddle the commit and the
withheld set; `ceremony.commit_v2` refuses such a checkpoint and leaves HEAD unchanged."""

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


def test_row_straddling_commit_and_withheld_is_named(plan_root):
    split = checkpoint_hunks.declared_sibling_split(
        plan_root, MESSAGE, ["schema/a.json", "other/c.py"], ["tool/b.py"],
    )

    assert split == [{"row": "C1", "committed": ["schema/a.json"], "withheld": ["tool/b.py"]}]


def test_row_wholly_committed_or_wholly_withheld_is_not_named(plan_root):
    assert checkpoint_hunks.declared_sibling_split(
        plan_root, MESSAGE, ["schema/a.json", "tool/b.py"], ["other/c.py"],
    ) == []
    assert checkpoint_hunks.declared_sibling_split(
        plan_root, MESSAGE, ["other/c.py"], ["schema/a.json", "tool/b.py"],
    ) == []


def test_unreadable_plan_or_empty_sets_name_nothing(plan_root):
    assert checkpoint_hunks.declared_sibling_split(plan_root, "no plan here", ["schema/a.json"], ["tool/b.py"]) == []
    assert checkpoint_hunks.declared_sibling_split(plan_root, MESSAGE, [], ["tool/b.py"]) == []
    assert checkpoint_hunks.declared_sibling_split(plan_root, MESSAGE, ["schema/a.json"], []) == []


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
def test_commit_refuses_a_row_split_by_a_foreign_hunk_and_head_is_unchanged(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == "tool/b.py" else HunkSplit(own=list(hunks))
        ),
    )
    head = _git(["rev-parse", "HEAD"], repo)

    result = _checkpoint(repo)

    assert result.get("committed") is not True, result
    assert "tool/b.py" in str(result), result
    assert "schema/a.json" in str(result), result
    assert _git(["rev-parse", "HEAD"], repo) == head


@pytest.mark.spawns_process
def test_commit_lands_every_declared_write_when_all_are_own(repo, monkeypatch):
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )

    result = _checkpoint(repo)

    assert result["committed"] is True, result
    assert "checkpoint_withheld_paths" not in result
