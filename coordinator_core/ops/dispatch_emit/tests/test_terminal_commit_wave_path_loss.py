"""A wide wave's commit leg receives every dirty declared path of its landing rows through the default
dispatch.terminal_commit entry point, whatever the held/landed split and across a refire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
_SHARED = "scan_truth/noun_report.py"


@pytest.fixture
def repo(tmp_path):
    return seed_repo(tmp_path / "repo")


def _fire(repo: Path, request: CommitRequest, **params) -> dict:
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8"
    )
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _wave(repo: Path) -> tuple:
    """16 declared paths: 14 rows of one path each, plus a held row sharing `_SHARED` with row 0."""
    names = [_SHARED] + [f"scan_truth/m{i:02d}.py" for i in range(1, 16)]
    (repo / "scan_truth").mkdir()
    for n in names:
        (repo / n).write_text("v1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    chunks = [ChunkCommit(id=f"C{i}", title=f"t{i}", paths=(n,)) for i, n in enumerate(names)]
    chunks.append(ChunkCommit(id="H", title="held", paths=(_SHARED, "scan_truth/m15.py")))
    return names, CommitRequest(chunks=tuple(chunks))


def _landed(repo: Path, since: str) -> set:
    out = _git(["diff", "--name-only", since, "HEAD"], repo).split()
    return {p for p in out if p.startswith("scan_truth/")}


def test_every_dirty_declared_path_of_the_landing_rows_reaches_the_commit(repo):
    names, request = _wave(repo)
    baseline = _git(["rev-parse", "HEAD"], repo).strip()
    dirty = [names[0], *names[2:14:2]]
    for n in dirty:
        (repo / n).write_text("v2\n", encoding="utf-8")
    out = _fire(repo, request, held=[{"chunk": "H", "reason": "dead"}])
    assert out["committed"] is True, out
    assert _landed(repo, baseline) == set(dirty)


def test_a_stranded_row_sharing_a_path_does_not_drop_the_landing_rows_dirty_paths(repo):
    names, request = _wave(repo)
    baseline = _git(["rev-parse", "HEAD"], repo).strip()
    dirty = [names[0], *names[1:8]]
    for n in dirty:
        (repo / n).write_text("v2\n", encoding="utf-8")
    out = _fire(repo, request, held=[], incomplete_chunks=["H"])
    assert out.get("committed") is True, out
    assert set(dirty) <= _landed(repo, baseline)


def test_a_refire_after_a_partial_landing_commits_the_remaining_dirty_paths(repo):
    names, request = _wave(repo)
    baseline = _git(["rev-parse", "HEAD"], repo).strip()
    first, second = names[1:9], names[9:15]
    for n in first:
        (repo / n).write_text("v2\n", encoding="utf-8")
    one = _fire(repo, request, held=[{"chunk": "H", "reason": "dead"}])
    assert one["committed"] is True, one
    for n in second:
        (repo / n).write_text("v2\n", encoding="utf-8")
    two = _fire(repo, request, held=[{"chunk": "H", "reason": "dead"}])
    assert two["committed"] is True, two
    assert _landed(repo, baseline) == set(first) | set(second)
    assert not two.get("nothing_to_commit")
