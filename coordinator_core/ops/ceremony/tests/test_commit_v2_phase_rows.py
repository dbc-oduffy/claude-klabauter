"""ceremony.commit_v2 writes one phase_time row per dispatch, abandoned or not."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _rows(repo: Path) -> list:
    sink = repo / ".git" / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    if not sink.exists():
        return []
    rows = [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in rows if r.get("kind") == "phase_time"]


def _call(repo: Path) -> dict:
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    return commit_v2._handler(
        {"paths": ["a.txt", "b.txt"], "message": "add a and b\n", "session_id": _SID},
        repo_root=repo / ".git",
    )


def test_two_file_commit_writes_one_phase_row(repo):
    result = _call(repo)
    assert result["committed"] is True
    rows = _rows(repo)
    assert len(rows) == 1
    row = rows[0]
    assert row["op"] == "ceremony.commit_v2"
    assert row["paths"] == 2
    assert row["abandoned"] is False
    assert "land.product" in row["phases_ms"]
    assert "pre_commit_gates" in row["phases_ms"]
    assert row["phases_ms"]["unbracketed"] >= 0
    assert "a.txt" not in json.dumps(row)


def test_abandoned_dispatch_still_writes_its_row(repo):
    ev = threading.Event()
    ev.set()
    token = dispatch_abandon.ACTIVE.set(ev)
    try:
        result = _call(repo)
    finally:
        dispatch_abandon.ACTIVE.reset(token)
    assert result["committed"] is False
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0]["abandoned"] is True


def test_early_refusal_still_writes_a_row(repo):
    result = commit_v2._handler({"paths": ["a.txt"], "message": ""}, repo_root=repo / ".git")
    assert result["committed"] is False
    assert len(_rows(repo)) == 1
