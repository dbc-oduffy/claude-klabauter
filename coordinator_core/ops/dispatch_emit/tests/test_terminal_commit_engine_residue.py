"""dispatch.terminal_commit names dirty engine-owned paths outside the landing in `engine_residue`,
never staging them and never repeating a path the reply already names."""

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
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "s", "slices": 1, "fixes": 0,
    "wave_reviews": reviewed()["wave_reviews"],
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


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


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _run(repo: Path, *declared: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(declared)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def test_residue_outside_the_pathspec_is_listed_and_not_committed(repo):
    _write(repo, "src/a.py", "a\n")
    _write(repo, "state/sizings/x.yaml")
    _write(repo, "docs/plans/p.seam.yaml")
    _write(repo, "docs/plans/p.md")
    out = _run(repo, "src/a.py")
    assert out["committed"] is True
    assert out["engine_residue"] == ["docs/plans/p.seam.yaml", "state/sizings/x.yaml"]
    tracked = _git(["ls-files"], repo).split()
    assert "src/a.py" in tracked
    assert "state/sizings/x.yaml" not in tracked and "docs/plans/p.seam.yaml" not in tracked


def test_residue_the_reply_already_names_is_not_repeated(repo):
    _write(repo, "state/sizings/a.yaml", "a\n")
    _write(repo, "state/sizings/y.yaml")
    _write(repo, "state/lessons/l.md")
    out = _run(repo, "state/sizings/a.yaml")
    assert "state/sizings/y.yaml" in out["undeclared_dirty"]
    assert "state/sizings/y.yaml" not in out["engine_residue"]
    assert "state/sizings/a.yaml" not in out["engine_residue"]
    assert out["engine_residue"] == ["state/lessons/l.md"]


def test_nothing_dirty_under_the_residue_roots_is_an_empty_list(repo):
    _write(repo, "src/a.py", "a\n")
    out = _run(repo, "src/a.py")
    assert out["engine_residue"] == []
