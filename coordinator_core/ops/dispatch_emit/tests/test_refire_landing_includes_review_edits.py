"""A landing carries every path the review's reverify record lists as edited, even when the
touch-record source misses it (a refire's review edited files of an already-checkpointed chunk)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000031"
EARLIER = "pkg/reach_claims.py"
OTHER = "pkg/other.py"
_REVIEW = {"integration_stem": "rev-stem", "slices": 1, "fixes": 1}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    (root / "pkg").mkdir()
    (root / EARLIER).write_text("v1\n", encoding="utf-8", newline="")
    (root / OTHER).write_text("v1\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "checkpoint"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    (root / EARLIER).write_text("v2\n", encoding="utf-8", newline="")
    (root / OTHER).write_text("v2\n", encoding="utf-8", newline="")
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    return root


def _run(repo: Path, reverify: dict) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [],
            "inline_review": {**_REVIEW, "reverify": reverify}, "session_id": SELF,
        },
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_a_reverify_edited_path_lands_though_the_touch_source_missed_it(repo):
    out = _run(repo, {"edited": [EARLIER]})
    assert out["committed"] is True, out
    assert EARLIER in _committed(repo)
    assert _git(["status", "--porcelain", "--", EARLIER], repo).strip() == ""


def test_a_dirty_path_the_reverify_record_omits_stays_uncommitted(repo):
    out = _run(repo, {"edited": [EARLIER]})
    assert out["committed"] is True, out
    assert OTHER not in _committed(repo)
    assert _git(["status", "--porcelain", "--", OTHER], repo).strip() != ""
