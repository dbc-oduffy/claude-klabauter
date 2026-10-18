"""Two live runs in one session: run A's terminal commit carries the session's own left-behind dirt
but never a path run B (same session, still live) declares it writes; once B's token is stale the
same path is carried, so the test discriminates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "other").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (root / "other" / "own.py").write_text("own\n", encoding="utf-8")
    (root / "other" / "b.py").write_text("b\n", encoding="utf-8")
    for rel in ("src/a.py", "other/own.py", "other/b.py"):
        session_scope.touch(SELF, rel, cwd=str(root))
    return root


def _live_run_b_declares(repo: Path, *writes: str) -> None:
    """Run B: a second live run of the same session, its manifest naming `writes`."""
    manifest_dir = repo / "state" / "warp" / "run-b"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": []}),
        encoding="utf-8",
    )
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)


def _run_a(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run-a.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run-a.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_a_live_sibling_runs_claimed_path_is_not_swept_into_the_terminal_commit(repo):
    _live_run_b_declares(repo, "other/b.py")
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["other/own.py", "src/a.py"]
    assert _git(["ls-files", "other/b.py"], repo).strip() == ""
    assert (repo / "other" / "b.py").read_text(encoding="utf-8") == "b\n"
    assert "other/b.py" in _git(["status", "--porcelain", "--untracked-files=all"], repo)


def test_control_a_stale_sibling_token_no_longer_excludes_its_path(repo):
    _live_run_b_declares(repo, "other/b.py")
    record_path = run_token._record_path(repo, "run-b")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["minted_at"] = 1.0
    record_path.write_text(json.dumps(record), encoding="utf-8")
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["other/b.py", "other/own.py", "src/a.py"]
