"""dispatch.terminal_commit lands a review edit made after the run's checkpoints to a file the run's
leg footprint declares, even when no request chunk names it; an undeclared peer edit stays dirty."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    LEG_FOOTPRINT_FILE,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c6"
DECLARED = "pred/declared.py"
PEER = "peer/undeclared.py"
_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1, "prep": {"slice_files": []},
    "wave_reviews": reviewed()["wave_reviews"],
}


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
    (root / "pred").mkdir()
    (root / "peer").mkdir()
    (root / DECLARED).write_text("V = 0\n", encoding="utf-8")
    (root / PEER).write_text("V = 0\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for n in (1, 2):
        (root / DECLARED).write_text(f"CHECKPOINT = {n}\n", encoding="utf-8")
        _git(["commit", "-q", "-am", f"checkpoint(wave {n}): 1 rows"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _stage(repo: Path, footprint: list | None) -> None:
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    if footprint is not None:
        (run_dir / LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": footprint}), encoding="utf-8")
    (repo / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8"
    )


def _fire(repo: Path, monkeypatch) -> dict:
    (repo / DECLARED).write_text("REVIEW_EDIT = 3\n", encoding="utf-8")
    (repo / PEER).write_text("PEER_EDIT = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        review_edits, "review_writes", lambda root, sid, since: {DECLARED: "agent-1", PEER: "agent-2"}
    )
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": SELF},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_footprint_declared_edit_after_checkpoints_lands_and_peer_edit_stays_dirty(repo, monkeypatch):
    _stage(repo, [DECLARED, "a.py"])
    out = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert DECLARED in _committed(repo)
    assert PEER not in _committed(repo)
    assert _git(["status", "--porcelain", "--", DECLARED], repo).strip() == ""
    assert _git(["status", "--porcelain", "--", PEER], repo).strip() != ""


def test_without_a_footprint_the_unclaimed_edit_is_not_landed(repo, monkeypatch):
    _stage(repo, None)
    out = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert DECLARED not in _committed(repo)
    assert _git(["status", "--porcelain", "--", DECLARED], repo).strip() != ""
