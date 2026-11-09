"""A dead run's own strand commits through dispatch.terminal_commit: rows of one run that write one
path are not foreign to each other, and the commit leg receives every dirty declared path."""

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


@pytest.fixture
def repo(tmp_path):
    return seed_repo(tmp_path / "repo")


def _stage_run(repo: Path, request: CommitRequest) -> str:
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json"


def _fire_manifest(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text(f"// emitted\n{_stage_run(repo, request)}\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _fire_inline(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["log", "--name-only", "--format="], repo)
    return sorted({ln for ln in out.splitlines() if ln.strip()} - {"README.md"})


def _shared_request(repo: Path) -> CommitRequest:
    for name in ("a.py", "b.py", "shared.py"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    return CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("a.py", "shared.py")),
        ChunkCommit(id="C2", title="t2", paths=("b.py", "shared.py")),
    ))


def test_a_held_row_of_the_same_run_sharing_a_path_does_not_refuse(repo):
    out = _fire_manifest(repo, _shared_request(repo), held=[{"chunk": "C2", "reason": "dead"}])
    assert out["committed"] is True, out
    landed = _committed(repo)
    assert "shared.py" in landed and "a.py" in landed
    assert "b.py" not in landed
    assert "b.py" in _git(["status", "--porcelain"], repo)


def test_a_held_row_of_a_run_without_a_manifest_still_refuses_the_shared_path(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    out = _fire_inline(repo, _shared_request(repo), held=[{"chunk": "C2", "reason": "dead"}])
    assert out["committed"] is False
    assert out["refused"] == "held-landed-overlap"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_held_spine_row_outside_the_marker_still_refuses_the_shared_path(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: H1\n  title: other\n  change_kind: code-edit\n  surface: a.py\n"
        "  writes:\n    - a.py\n  disposition: open\n  deferred: false\n```\n",
        encoding="utf-8", newline="\n",
    )
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path="docs/plan.md",
    )
    out = _fire_manifest(repo, request, held=[{"chunk": "H1", "reason": "other"}], plan_path="docs/plan.md")
    assert out["committed"] is False
    assert out["refused"] == "held-landed-overlap"


def test_a_held_only_path_of_the_same_run_stays_uncommitted(repo):
    out = _fire_manifest(repo, _shared_request(repo), held=[{"chunk": "C2", "reason": "dead"}])
    assert out["committed"] is True, out
    assert out["stranded"].get("C2") is not None
    assert "b.py" not in _committed(repo)


def test_the_commit_leg_receives_every_dirty_declared_path_of_a_wide_wave(repo):
    names = [f"m{i:02d}.py" for i in range(16)]
    for n in names:
        (repo / n).write_text("v1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    baseline = _git(["rev-parse", "HEAD"], repo).strip()
    dirty = names[::2][:7]
    for n in dirty:
        (repo / n).write_text("v2\n", encoding="utf-8")
    request = CommitRequest(chunks=tuple(
        ChunkCommit(id=f"C{i}", title=f"t{i}", paths=(n,)) for i, n in enumerate(names)
    ))
    out = _fire_manifest(repo, request)
    assert out["committed"] is True, out
    since = _git(["diff", "--name-only", baseline, "HEAD"], repo).split()
    assert sorted(p for p in since if p.startswith("m")) == sorted(dirty)
