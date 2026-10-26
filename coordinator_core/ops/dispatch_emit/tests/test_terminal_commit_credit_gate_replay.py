"""Replay of the d5ade3507c run shape through dispatch.terminal_commit: a terminal commit whose every
product chunk is stranded refuses naming the chunks; a disentangled sibling still lands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c2"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()

C1_PATHS = (
    "pkg/emit.py",
    "pkg/tests/test_checkpoint_lines.py",
    "pkg/tests/test_emit_extra.py",
)
C2_PATHS = (
    "pkg/ask_stage.py",
    "pkg/spine_read.py",
    "pkg/tests/test_ask_stage_enum.py",
    "pkg/tests/test_spine_read.py",
    "pkg/tests/test_ask_stage_extra.py",
)
DOCS_PATH = "docs/delivery/run.md"
SIZING_PATH = "state/sizings/run.yaml"
REVIEW_PATH = C1_PATHS[1]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg" / "tests").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(review_edits, "review_writes", lambda r, sid, since: {REVIEW_PATH: "agent-1"})
    return root


def _write(root: Path, rel: str, body: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body, encoding="utf-8")


def _shape(root: Path, ask_stage_body: str) -> None:
    for rel in C1_PATHS:
        _write(root, rel, "A = 1\n")
    for rel in C2_PATHS:
        _write(root, rel, "B = 1\n")
    _write(root, "pkg/ask_stage.py", ask_stage_body)
    _write(root, DOCS_PATH, "What shipped: emit, ask_stage.\n")
    _write(root, SIZING_PATH, "sizing: 1\n")
    chunks = []
    for cid, paths in (("C1", C1_PATHS), ("C2", C2_PATHS), ("docs", (DOCS_PATH,)), ("sizing", (SIZING_PATH,))):
        _write(root, f"report-{cid}.md", "BLOCKED: no runtime\n" if cid == "C1" else "DONE: ok\n")
        chunks.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=f"report-{cid}.md"))
    _write(root, "run.mjs", "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n")


def _call(root: Path) -> dict:
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": ["C1"], "inline_review": _REVIEWED,
            "session_id": SELF,
        },
        repo_root=root / ".git",
    )


def _dirty(root: Path, rel: str) -> str:
    return _git(["status", "--porcelain", "--untracked-files=all", "--", rel], root).strip()


def test_every_product_chunk_stranded_refuses_naming_c1_and_c2(repo):
    _shape(repo, "from pkg.emit import A\n")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo)
    assert out["committed"] is False, out
    assert out["refused"] == "no-credited-chunk-landed"
    assert out["stranded"]["C1"] == sorted(C1_PATHS)
    assert out["stranded"]["C2"] == sorted(C2_PATHS)
    assert out["stranded_residue"] == {"C1": [REVIEW_PATH]}
    assert _git(["rev-parse", "HEAD"], repo) == head
    for rel in (*C1_PATHS, *C2_PATHS):
        assert _dirty(repo, rel) != "", rel


def test_disentangled_c2_lands_and_c1_review_edit_is_not_carried(repo):
    _shape(repo, "B = 2\n")
    out = _call(repo)
    assert out["committed"] is True, out
    assert set(out["stranded"]) == {"C1"}
    assert out["stranded"]["C1"] == sorted(C1_PATHS)
    assert "C2" in out["chunks_committed"]
    landed = set(_git(["ls-files"], repo).split())
    assert set(C2_PATHS) <= landed
    assert not (set(C1_PATHS) & landed)
    assert "Review-Residue" not in _git(["log", "-1", "--format=%B"], repo)
    assert _dirty(repo, REVIEW_PATH) != ""
