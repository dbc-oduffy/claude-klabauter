"""dispatch.terminal_commit's commit leg declares the run session's recorded fingerprints to
``ceremony.commit_v2`` and refuses a path a peer rewrote after the run's last recorded write."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_TOKENS: dict = {}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _script(repo: Path) -> str:
    request = CommitRequest(chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _record(repo: Path, rel: str, digest) -> None:
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="w",
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fire(repo: Path, monkeypatch) -> tuple[dict, list]:
    from coordinator_core.ops.dispatch_emit import run_token

    script = _script(repo)
    token = _TOKENS.get(repo) or _TOKENS.setdefault(repo, run_token.mint(repo, "run-1", script))
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": REVIEWED,
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )
    return out, seen


def test_drift_since_the_runs_last_recorded_write_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    head = _git(["rev-parse", "HEAD"], repo)

    out, seen = _fire(repo, monkeypatch)

    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not out.get("receipts")
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_a_refused_run_leaves_no_receipts_and_can_refire(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is False

    (repo / "a.py").write_text("A\n", encoding="utf-8")
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is True, out


def test_matching_bytes_commit(repo, monkeypatch):
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_a_hashless_newest_event_carries_no_expectation(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    _record(repo, "a.py", None)
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert "expected_content_sha256" not in seen[0]


def test_only_committed_declared_paths_enter_the_expectation_map(repo, monkeypatch):
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    _record(repo, "README.md", _sha("not-the-readme\n"))
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    declared = set(seen[0]["expected_content_sha256"])
    assert declared == {"a.py"}
    assert declared.isdisjoint(out["receipts"])
