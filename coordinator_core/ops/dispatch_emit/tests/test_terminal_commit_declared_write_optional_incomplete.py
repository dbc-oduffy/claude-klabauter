"""An absent declared write marked optional, or one on a chunk listed in params.incomplete_chunks,
never refuses declared-write-missing; an absent required write on an unlisted DONE row still does."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}
_SID = "11111111-2222-3333-4444-555555555555"
PRESENT = "tools/names.py"
ABSENT = "tests/fixtures/failed_head_real.npz"


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
    (root / PRESENT).parent.mkdir(parents=True)
    (root / PRESENT).write_text("old\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _stage_run(root: Path, *, optional: tuple = ()) -> str:
    (root / PRESENT).write_text("new\n", encoding="utf-8")
    (root / "report-C2.md").write_text("DONE: built\n", encoding="utf-8")
    chunk = ChunkCommit(
        id="C2", title="names", paths=(PRESENT, ABSENT), report="report-C2.md", optional=optional,
    )
    other = ChunkCommit(id="C4", title="legs", paths=("legs.md",))
    request = CommitRequest(chunks=(chunk, other))
    (root / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _call(root: Path, script: str, incomplete: list) -> dict:
    params = {
        "script_path": script, "incomplete_chunks": incomplete, "inline_review": _REVIEWED,
        "session_id": _SID,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _tree(root: Path) -> set:
    return set(_git(["ls-tree", "-r", "--name-only", "HEAD"], root).split())


def test_a_required_false_absent_write_lands_the_row(repo):
    out = _call(repo, _stage_run(repo, optional=(ABSENT,)), ["C4"])

    assert out.get("refused") != "declared-write-missing", out
    assert out["committed"] is True, out
    assert "new" in _git(["show", f"HEAD:{PRESENT}"], repo)


def test_a_chunk_listed_in_incomplete_chunks_clears_the_check_though_partly_present(repo):
    out = _call(repo, _stage_run(repo), ["C4", "C2"])

    assert out["committed"] is True, out
    assert not out.get("missing_declared"), out


def test_a_required_absent_write_on_an_unlisted_done_row_still_refuses(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, _stage_run(repo), ["C4"])

    assert out["committed"] is False, out
    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": [ABSENT]}, out
    assert _git(["rev-parse", "HEAD"], repo) == head
