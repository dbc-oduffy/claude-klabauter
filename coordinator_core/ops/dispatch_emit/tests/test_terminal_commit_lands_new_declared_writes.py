"""A DONE row's declared writes that are new untracked files land in the terminal commit with its
tracked ones, or the commit refuses naming the missing path; it never lands a partial row."""

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
TRACKED = ("tools/names.py", "tools/test_names.py")
NEW = (
    "state/naming/head-names.v1.jsonl",
    "state/naming/head-names-counts.v1.json",
    "state/naming/head-names.pin.json",
)


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
    for rel in TRACKED:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("old\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _stage_run(root: Path, *, new: tuple = NEW, other_chunk: bool = True) -> str:
    for rel in TRACKED:
        (root / rel).write_text("new\n", encoding="utf-8")
    for rel in new:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text('{"v": 1}\n', encoding="utf-8")
    (root / "report-C2.md").write_text("DONE: names built\n", encoding="utf-8")
    chunks = [ChunkCommit(id="C2", title="names", paths=(*TRACKED, *NEW), report="report-C2.md")]
    if other_chunk:
        chunks.append(ChunkCommit(id="C4", title="legs", paths=("legs.md",)))
    request = CommitRequest(chunks=tuple(chunks))
    (root / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _call(root: Path, script: str, **extra) -> dict:
    params = {
        "script_path": script, "incomplete_chunks": ["C4"], "inline_review": _REVIEWED,
        "session_id": _SID, **extra,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _tree(root: Path) -> set:
    return set(_git(["ls-tree", "-r", "--name-only", "HEAD"], root).split())


def test_new_declared_files_land_beside_the_tracked_ones(repo):
    out = _call(repo, _stage_run(repo))

    assert out["committed"] is True, out
    assert set(TRACKED) | set(NEW) <= _tree(repo)
    assert _git(["status", "--porcelain", "-uall", "--", *NEW, *TRACKED], repo) == ""


def test_a_declared_new_file_that_does_not_exist_refuses_naming_it(repo):
    script = _stage_run(repo)
    (repo / NEW[1]).unlink()
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, script)

    assert out["committed"] is False, out
    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": [NEW[1]]}, out
    assert NEW[1] in out["error"], out
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert NEW[0] not in _tree(repo)


def test_a_row_without_a_done_report_keeps_the_quiet_drop(repo):
    script = _stage_run(repo)
    (repo / "report-C2.md").write_text("PARTIAL: names half built\nNot done: counts file\n", encoding="utf-8")
    (repo / NEW[1]).unlink()

    out = _call(repo, script, incomplete_chunks=["C4", "C2"])

    assert out.get("refused") != "declared-write-missing", out
    assert out["committed"] is False and out["nothing_to_commit"] is True, out
    assert NEW[1] in out["stranded"]["C2"], out
    assert NEW[0] not in _tree(repo)


def test_a_second_missing_declared_write_refuses_without_landing_the_present_ones(repo):
    script = _stage_run(repo)
    (repo / NEW[2]).unlink()
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, script)

    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": [NEW[2]]}, out
    assert _git(["rev-parse", "HEAD"], repo) == head
