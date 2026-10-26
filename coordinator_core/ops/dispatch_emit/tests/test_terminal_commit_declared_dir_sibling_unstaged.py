"""An unrelated untracked file beside a declared path stays unstaged and out of the product commit
dispatch.terminal_commit makes, whether it is unclaimed, declared by a live peer run's manifest and listed in this run's slice, or
claimed by this session."""

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
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()


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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    (root / "src" / "stray.py").write_text("stray\n", encoding="utf-8", newline="")
    return root


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _in_commit(repo: Path, sha: str) -> list:
    out = _git(["show", "--name-only", "--format=", sha], repo)
    return sorted(ln for ln in out.splitlines() if ln.strip())


def test_an_unclaimed_sibling_is_in_no_commit_and_stays_untracked(repo):
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _in_commit(repo, out["sha"])
    assert "src/stray.py" in _git(["status", "--porcelain", "-uall", "--", "src"], repo)
    assert "?? src/stray.py" in _git(["status", "--porcelain", "-uall", "--", "src"], repo)
    assert "src/stray.py" in out["undeclared_dirty"]


def test_a_sibling_a_concurrent_runs_slice_lists_is_not_in_the_product_commit(repo):
    session_core.init(PEER, cwd=str(repo))
    manifest_dir = repo / "state" / "warp" / "run-b"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["src/stray.py"]}], "doc_writes": []}),
        encoding="utf-8",
    )
    run_token.mint(repo, "run-b", "b.mjs", session_id=PEER)
    reviewed = {**_REVIEWED, "prep": {"slice_files": ["src/stray.py"]}}
    out = _run(repo, session_id=SELF, inline_review=reviewed)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _in_commit(repo, out["sha"])


def test_a_session_claimed_sibling_is_not_in_the_product_commit(repo):
    session_scope.touch(SELF, "src/stray.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _in_commit(repo, out["sha"])
