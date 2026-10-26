"""dispatch.terminal_commit never credits a chunk whose declared code stays uncommitted: stranded
paths take no side route (D1), a commit landing no product chunk is refused (D3), and every credited
chunk's changed declared write must be in the commit (D2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import credit_gate, review_edits, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000021"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()
C1_TEST = "src/tests/test_c1.py"


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
    (root / "src" / "tests").mkdir(parents=True)
    return root


def _write(repo: Path, rel: str) -> None:
    (repo / rel).write_text("x\n", encoding="utf-8")


def _call(repo: Path, chunks, incomplete, **params) -> dict:
    request = CommitRequest(chunks=tuple(chunks))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": incomplete, "inline_review": _REVIEWED,
            "session_id": SELF, **params,
        },
        repo_root=repo / ".git",
    )


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def _chunks() -> list:
    return [
        ChunkCommit(id="C1", title="t1", paths=("src/c1.py", C1_TEST)),
        ChunkCommit(id="docs", title="docs", paths=("docs/note.md",)),
    ]


def _dirty_all(repo: Path) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    for rel in ("src/c1.py", C1_TEST, "docs/note.md"):
        _write(repo, rel)


def test_review_edit_on_a_stranded_path_stays_uncommitted_and_is_named(repo, monkeypatch):
    _write(repo, "src/c2.py")
    _write(repo, C1_TEST)
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {C1_TEST: "agent-1"})
    chunks = [
        ChunkCommit(id="C1", title="t1", paths=("src/c1.py", C1_TEST)),
        ChunkCommit(id="C2", title="t2", paths=("src/c2.py",)),
    ]
    out = _call(repo, chunks, ["C1"])
    assert out["committed"] is True, out
    assert out["stranded_residue"] == {"C1": [C1_TEST]}
    assert "review_residue" not in out
    assert C1_TEST not in _committed(repo)
    assert "Review-Residue" not in _git(["log", "-1", "--format=%B"], repo)
    assert _git(["status", "--porcelain", "--", C1_TEST], repo).strip().endswith(C1_TEST)


def test_every_product_chunk_stranded_refuses_and_moves_nothing(repo):
    _dirty_all(repo)
    head = _head(repo)
    out = _call(repo, _chunks(), ["C1"])
    assert out["committed"] is False
    assert out["refused"] == credit_gate.REFUSED_NO_CREDITED_LANDING
    assert out["stranded"] == {"C1": ["src/c1.py", C1_TEST]}
    assert "C1" in out["error"] and "src/c1.py" in out["error"]
    assert "next" in out
    assert _head(repo) == head


def test_an_earlier_landed_product_chunk_lets_the_bookkeeping_commit_stand(repo):
    _write(repo, "src/c1.py")
    _git(["add", "src/c1.py"], repo)
    _git(["commit", "-q", "-m", "c1 landed by checkpoint"], repo)
    (repo / "docs").mkdir()
    _write(repo, "docs/note.md")
    _write(repo, "src/c2.py")
    chunks = [
        ChunkCommit(id="C1", title="t1", paths=("src/c1.py",)),
        ChunkCommit(id="C2", title="t2", paths=("src/c2.py",)),
        ChunkCommit(id="docs", title="docs", paths=("docs/note.md",)),
    ]
    out = _call(repo, chunks, ["C2"], landed_chunks=["C1"])
    assert out["committed"] is True, out
    assert out["stranded"] == {"C2": ["src/c2.py"]}
    assert "docs/note.md" in _committed(repo)


def test_a_credited_path_dropped_by_a_filter_refuses_naming_chunk_and_path(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import cross_repo_write_refusal

    real = cross_repo_write_refusal.split_sibling_paths

    def drops_c2(paths, root):
        kept, siblings = real(paths, root)
        return [p for p in kept if p != "src/c2.py"], siblings

    monkeypatch.setattr(cross_repo_write_refusal, "split_sibling_paths", drops_c2)
    _write(repo, "src/c1.py")
    _write(repo, "src/c2.py")
    chunks = [
        ChunkCommit(id="C1", title="t1", paths=("src/c1.py",)),
        ChunkCommit(id="C2", title="t2", paths=("src/c2.py",)),
    ]
    head = _head(repo)
    out = _call(repo, chunks, [])
    assert out["committed"] is False
    assert out["refused"] == credit_gate.REFUSED_CREDITED_UNCOMMITTED
    assert out["uncommitted"] == {"C2": ["src/c2.py"]}
    assert "C2" in out["error"] and "src/c2.py" in out["error"]
    assert _head(repo) == head
