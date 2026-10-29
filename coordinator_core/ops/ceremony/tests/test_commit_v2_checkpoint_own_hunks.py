"""A wave checkpoint through `ceremony.commit_v2` lands the run's own hunks only and withholds
paths whose hunks no baseline attributes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cko-self"
PLAN = "state/mise-inventory/run.spine.md"
BASE_TEXT = "".join(f"line{i}\n" for i in range(1, 11))


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _message(base: str) -> str:
    return f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"


def _params(repo: Path, paths: list) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return {
        "paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True,
        "message": _message(base),
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "shared.txt").write_bytes(BASE_TEXT.encode())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    return root


def _edit_shared(repo: Path) -> None:
    lines = BASE_TEXT.splitlines(keepends=True)
    lines[1], lines[8] = "OWN\n", "FOREIGN\n"
    (repo / "shared.txt").write_bytes("".join(lines).encode())


def test_checkpoint_lands_own_hunk_and_leaves_foreign_hunk_in_worktree(repo, monkeypatch):
    _edit_shared(repo)
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: HunkSplit(own=[h for h in hunks if h.old_start == 2]),
    )

    result = _call(repo, _params(repo, ["shared.txt"]))

    assert result["committed"] is True, result
    landed = _git(["show", "HEAD:shared.txt"], repo)
    assert "OWN" in landed and "FOREIGN" not in landed
    assert "FOREIGN" in (repo / "shared.txt").read_text()
    assert "checkpoint_withheld_paths" not in result


def test_checkpoint_withholds_path_with_unattributed_hunk(repo, monkeypatch):
    _edit_shared(repo)
    (repo / "mine.txt").write_text("mine\n")
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: HunkSplit(own=hunks[:1], unattributed=hunks[1:]),
    )

    result = _call(repo, _params(repo, ["shared.txt", "mine.txt"]))

    assert result["committed"] is True, result
    assert result["checkpoint_withheld_paths"] == ["shared.txt"]
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"mine.txt"}
    assert "FOREIGN" in (repo / "shared.txt").read_text()


def test_checkpoint_with_every_path_withheld_commits_nothing(repo, monkeypatch):
    _edit_shared(repo)
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: HunkSplit(unattributed=list(hunks)),
    )
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(repo, _params(repo, ["shared.txt"]))

    assert result.get("committed") is not True
    assert result["checkpoint_withheld_paths"] == ["shared.txt"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_non_checkpoint_commit_lands_the_file_whole(repo):
    _edit_shared(repo)

    result = _call(repo, {"paths": ["shared.txt"], "message": "plain"})

    assert result["committed"] is True, result
    assert "FOREIGN" in _git(["show", "HEAD:shared.txt"], repo)
    assert "checkpoint_withheld_paths" not in result
