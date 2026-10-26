"""dispatch.terminal_commit carries the sizing its plan cites as `sizing_object:`.

The warp ask arm rewrites the sizing during the run (status routed, plan link);
no chunk declares it, so without the ride-along it stayed dirty after the commit.
"""

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

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()
_SIZING = "state/sizings/2026-10-06-x.yaml"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "docs").mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / _SIZING).write_text("status: draft\n", encoding="utf-8")
    (root / "docs" / "plan.md").write_text(
        f"---\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8"
    )
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, plan_path: str) -> dict:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),),
        plan_path=plan_path,
    )
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )


def test_the_cited_sizing_rewrite_lands_in_the_terminal_commit(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / _SIZING).write_text("status: routed\nplan: docs/plan.md\n", encoding="utf-8")

    out = _fire(repo, "docs/plan.md")

    assert out["committed"] is True
    # commit_v2 splits product from process paths (b4333bfd51): the sizing (state/) rides HEAD.
    landed = _git(["show", "--name-only", "--format=", out["sha"]], repo) + _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert _SIZING in landed
    assert _git(["status", "--porcelain", "--", _SIZING], repo) == ""


def test_a_sizing_outside_state_sizings_is_never_carried(repo):
    (repo / "other.yaml").write_text("x: 1\n", encoding="utf-8")
    _git(["add", "other.yaml"], repo)
    _git(["commit", "-q", "-m", "other"], repo)
    (repo / "docs" / "plan.md").write_text(
        "---\nsizing_object: other.yaml\n---\nbody\n", encoding="utf-8"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "other.yaml").write_text("x: 2\n", encoding="utf-8")

    out = _fire(repo, "docs/plan.md")

    assert out["committed"] is True
    assert "other.yaml" not in _git(["show", "--name-only", "--format=", out["sha"]], repo)


def _ask_fire(repo: Path, plan_rel: str) -> dict:
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=plan_rel
    )
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(
        f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8"
    )
    return terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )


@pytest.fixture
def shipping(monkeypatch, repo):
    def ship(worktree_root, repo_root, plan_path):
        (worktree_root / _SIZING).write_text("status: shipped\n", encoding="utf-8")
        return {"sizing_shipped": _SIZING}

    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", ship)
    monkeypatch.setattr(terminal_commit, "_write_resolution", lambda *a, **k: {})
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    return repo


def test_an_ignored_plan_run_still_lands_the_shipped_sizing(shipping):
    repo = shipping
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    plan = repo / "scratch" / "plan.md"
    plan.parent.mkdir()
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")

    out = _ask_fire(repo, "scratch/plan.md")

    assert out["committed"] is True, out
    assert "coded_stamp_error" not in out
    assert _git(["show", "HEAD:" + _SIZING], repo) == "status: shipped\n"
    assert _git(["status", "--porcelain", "--", _SIZING], repo) == ""


def test_a_stamp_commit_that_cannot_land_the_sizing_fails_the_run(shipping, monkeypatch):
    repo = shipping
    real = terminal_commit._stamp_coded_commit

    def refused(*args, **kwargs):
        out = real(lambda params, root: {"committed": False, "error": "denied"}, *args[1:], **kwargs)
        return out

    monkeypatch.setattr(terminal_commit, "_stamp_coded_commit", refused)
    out = _ask_fire(repo, "docs/plan.md")

    assert out["ok"] is False
    assert _SIZING in out["error"]
