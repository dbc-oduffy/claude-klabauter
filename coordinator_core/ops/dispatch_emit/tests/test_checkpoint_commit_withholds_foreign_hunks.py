"""Two runs dirty in one tree: run A's wave checkpoint through `ceremony.commit_v2` lands A's new
file, withholds the new file holding run B's content, and leaves B's bytes in the worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution, hunk_provenance, peer_hunk_gate
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cfh-self"
PLAN = "state/mise-inventory/run.spine.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "seed.txt").write_text("seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "runA")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(halted_hunk_attribution, "_baseline", lambda root, run: {"mine.txt": None})
    return root


def _params(repo: Path, paths: list) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return {
        "paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True,
        "message": f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n",
    }


def test_checkpoint_lands_own_new_file_and_withholds_the_peer_new_file(repo):
    (repo / "mine.txt").write_bytes(b"mine\n")
    (repo / "theirs.txt").write_bytes(b"run B wiring\n")

    result = commit_v2._handler(_params(repo, ["mine.txt", "theirs.txt"]), repo_root=repo / ".git")

    assert result["committed"] is True, result
    assert result["checkpoint_withheld_paths"] == ["theirs.txt"]
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"mine.txt"}
    assert (repo / "theirs.txt").read_bytes() == b"run B wiring\n"
    assert "?? theirs.txt" in _git(["status", "--porcelain", "-uall"], repo)


def test_checkpoint_with_only_a_peer_new_file_commits_nothing(repo):
    (repo / "theirs.txt").write_bytes(b"run B wiring\n")
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = commit_v2._handler(_params(repo, ["theirs.txt"]), repo_root=repo / ".git")

    assert result.get("committed") is not True
    assert result["checkpoint_withheld_paths"] == ["theirs.txt"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_the_new_file_gate_is_the_shared_unproduced_hunk_check(repo, monkeypatch):
    seen: list = []
    real = hunk_provenance.unproduced_hunks

    def spy(root, run_id, paths, **kw):
        seen.append((run_id, list(paths)))
        return real(root, run_id, paths, **kw)

    monkeypatch.setattr(hunk_provenance, "unproduced_hunks", spy)
    (repo / "mine.txt").write_bytes(b"mine\n")

    result = commit_v2._handler(_params(repo, ["mine.txt"]), repo_root=repo / ".git")

    assert result["committed"] is True, result
    assert seen == [("runA", ["mine.txt"])]


def test_a_real_manifest_baseline_naming_the_new_file_lands_it(repo, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    monkeypatch.undo()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "runA")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    record = repo / RECORD_DIR_ROOT / "runA"
    record.mkdir(parents=True)
    (record / "manifest.json").write_text(json.dumps({"write_baseline": {"mine.txt": None}}), encoding="utf-8")
    (repo / "mine.txt").write_bytes(b"mine\n")
    (repo / "theirs.txt").write_bytes(b"run B wiring\n")

    result = commit_v2._handler(_params(repo, ["mine.txt", "theirs.txt"]), repo_root=repo / ".git")

    assert result["committed"] is True, result
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"mine.txt"}
    assert result["checkpoint_withheld_paths"] == ["theirs.txt"]
