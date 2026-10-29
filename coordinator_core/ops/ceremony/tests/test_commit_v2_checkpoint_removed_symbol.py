"""A wave checkpoint through `ceremony.commit_v2` is refused while it removes a symbol that a
file outside its staged set still imports."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cks2-self"
PLAN = "state/mise-inventory/run.spine.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _params(repo: Path, paths: list) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return {
        "paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True,
        "message": (
            f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"
        ),
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "held.py").write_text("def _paths_of():\n    return 1\n\n\ndef keep():\n    return 2\n")
    (root / "pkg" / "user.py").write_text("from pkg.held import _paths_of\n\nX = _paths_of()\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(
        checkpoint_hunks, "checkpoint_own_blobs", lambda root, run, paths, message="": ({}, [])
    )
    return root


def _remove_symbol(repo: Path) -> None:
    (repo / "pkg" / "held.py").write_text("def keep():\n    return 2\n")


def test_checkpoint_removing_a_symbol_an_outside_caller_uses_is_refused(repo):
    _remove_symbol(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(repo, _params(repo, ["pkg/held.py"]))

    assert result.get("committed") is not True
    assert "_paths_of" in result["error"] and "pkg/user.py" in result["error"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_checkpoint_staging_the_caller_update_lands(repo):
    _remove_symbol(repo)
    (repo / "pkg" / "user.py").write_text("X = 1\n")

    result = _call(repo, _params(repo, ["pkg/held.py", "pkg/user.py"]))

    assert result["committed"] is True, result


def test_non_checkpoint_commit_is_not_symbol_checked(repo):
    _remove_symbol(repo)

    result = _call(repo, {"paths": ["pkg/held.py"], "message": "plain"})

    assert result["committed"] is True, result
