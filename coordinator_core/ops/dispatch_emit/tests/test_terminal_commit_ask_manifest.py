"""dispatch.terminal_commit reading the commit request through an ask run's manifest."""

from __future__ import annotations

import json
import subprocess
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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


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


def _stage(repo: Path, marker_path: str | None = None) -> str:
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1",
        rows=(),
        review_declared_paths=(),
        marker_path=marker_path or f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return f"{RUN_DIR_ROOT}/run1/manifest.json"


def _call(repo: Path, script_text: str) -> dict:
    (repo / "run.mjs").write_text(script_text, encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def test_manifest_sourced_request_commits_done_chunk_paths(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    manifest = _stage(repo)
    out = _call(repo, f"// emitted\n{ASK_MANIFEST_MARKER}{manifest}\n")
    assert out["committed"] is True
    assert out["chunks_committed"] == ["C1"]
    shown = subprocess.run(
        ["git", "show", "--stat", "--format=%s", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert "a.py" in shown


def test_both_markers_refuses(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    manifest = _stage(repo)
    inline = render_marker(CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),)))
    out = _call(repo, f"{inline}\n{ASK_MANIFEST_MARKER}{manifest}\n")
    assert out["committed"] is False
    assert out["refused"] == "both-markers"


def test_manifest_path_escaping_run_root_refuses(repo):
    _stage(repo)
    (repo / "outside.json").write_text("{}", encoding="utf-8")
    out = _call(repo, f"{ASK_MANIFEST_MARKER}outside.json\n")
    assert out["committed"] is False
    assert out["refused"] == "manifest-escapes-run-root"


def test_malformed_manifest_request_refuses(repo):
    manifest = _stage(repo)
    request_file = repo / RUN_DIR_ROOT / "run1" / "commit-request.txt"
    request_file.write_text(request_file.read_text(encoding="utf-8") * 2, encoding="utf-8")
    out = _call(repo, f"{ASK_MANIFEST_MARKER}{manifest}\n")
    assert out["committed"] is False
    assert out["refused"] == "malformed-request"


def test_marker_path_escaping_run_root_refuses(repo):
    (repo / "elsewhere.txt").write_text("x", encoding="utf-8")
    manifest = _stage(repo, marker_path="elsewhere.txt")
    out = _call(repo, f"{ASK_MANIFEST_MARKER}{manifest}\n")
    assert out["committed"] is False
    assert out["refused"] == "manifest-escapes-run-root"
