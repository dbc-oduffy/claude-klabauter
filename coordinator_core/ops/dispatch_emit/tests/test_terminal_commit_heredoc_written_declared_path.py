"""A declared path written through plain file I/O (the Bash-heredoc fallback for a harness-denied
Write) leaves no touch record; the terminal commit still lands it. When the same path carries an
earlier recorded Write and is then overwritten with no new record, the commit reports blob drift."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb7"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_write(repo: Path, rel: str, digest: str) -> None:
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="w",
    )


def _fire(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C7", title="t7", paths=("report.md",)),))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, "run-1", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": REVIEWED,
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )


def test_a_declared_path_written_with_no_touch_record_lands(repo):
    (repo / "report.md").write_text("heredoc body\n", encoding="utf-8", newline="")

    out = _fire(repo)

    assert out["committed"] is True, out
    assert not out.get("blob_drift")
    assert out.get("refused") is None
    assert _git(["show", "HEAD:report.md"], repo) == "heredoc body"


def test_a_recorded_write_then_a_plain_overwrite_is_reported_as_blob_drift(repo):
    (repo / "report.md").write_text("overwritten\n", encoding="utf-8", newline="")
    _record_write(repo, "report.md", _sha("recorded\n"))
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "report.md"
    assert _git(["rev-parse", "HEAD"], repo) == head
