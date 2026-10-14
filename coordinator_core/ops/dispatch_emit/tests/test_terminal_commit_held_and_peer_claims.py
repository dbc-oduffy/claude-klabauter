"""dispatch.terminal_commit never lands a held chunk's declared writes, and refuses a path
carrying a live peer session's write claim."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER = "held-peer"
SELF = "held-self"
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
    return root


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _two_chunks(repo: Path, second_paths=("b.py",)) -> CommitRequest:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    return CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=tuple(second_paths)),
        )
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_held_chunk_paths_are_subtracted_from_the_commit(repo):
    # C2 is not in incomplete_chunks: the held list alone must keep b.py out.
    out = _run(repo, _two_chunks(repo), held=[{"chunk": "C2", "reason": "halted"}])
    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py"]
    assert out["stranded"] == {"C2": ["b.py"]}
    assert out["incomplete_reasons"] == {"C2": "held: halted"}


def test_held_spine_writes_are_subtracted_even_when_the_marker_lacks_the_chunk(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: H1\n  title: held\n  change_kind: code-edit\n  surface: docs/held.md\n"
        "  writes:\n    - docs/held.md\n  disposition: open\n  deferred: false\n```\n",
        encoding="utf-8", newline="\n",
    )
    (repo / "docs" / "held.md").write_text("held\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path="docs/plan.md",
    )
    out = _run(
        repo, request, held=[{"chunk": "H1", "reason": "halted"}],
        plan_path="docs/plan.md",
    )
    assert out["committed"] is True, out
    assert "docs/held.md" not in _committed(repo)
    assert "a.py" in _committed(repo)


def test_a_path_declared_by_held_and_landed_chunks_refuses(repo):
    request = _two_chunks(repo, second_paths=("b.py", "a.py"))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request, held=[{"chunk": "C2", "reason": "halted"}])
    assert out["committed"] is False
    assert out["refused"] == "held-landed-overlap"
    assert out["overlap"] == {"a.py": {"held": ["C2"], "landed": ["C1"]}}
    assert "a.py" in out["error"] and "C1" in out["error"] and "C2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_live_peer_write_claim_refuses_the_commit(repo):
    request = _two_chunks(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request)
    assert out["committed"] is False
    assert out["refused"] == "peer-claim"
    assert out["peer_claims"] == [{"path": "a.py", "session": PEER}]
    assert "a.py" in out["error"] and PEER in out["error"] and "Wait for the peer" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_runs_own_session_claim_still_commits(repo):
    request = _two_chunks(repo)
    session_core.init(SELF, cwd=str(repo))
    session_scope.touch(SELF, "a.py", cwd=str(repo))
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py", "b.py"]
    assert "Absorbed-Peer-Claim" not in _git(["log", "-1", "--format=%B"], repo)
