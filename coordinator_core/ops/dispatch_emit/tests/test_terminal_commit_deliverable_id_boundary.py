"""terminal_commit refuses a marker Deliverable-Id that is not dlv-shaped or
differs from its plan's, before any write."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    plan_deliverable_id,
    render_marker,
    valid_deliverable_id,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_ID = "dlv-plan111"


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


def _run(
    repo: Path,
    marker_id: Optional[str],
    plan_id: Optional[str],
    plan_exists=True,
    chunk_path: str = "a.py",
    sibling_plan_id: Optional[str] = None,
):
    plan_rel = "docs/plan.md"
    if plan_exists:
        (repo / "docs").mkdir(exist_ok=True)
        fm = f"deliverable_id: {plan_id}\n" if plan_id else ""
        (repo / plan_rel).write_text(f"---\n{fm}title: t\n---\nbody\n", encoding="utf-8")
    if sibling_plan_id:
        (repo / chunk_path).write_text(
            f"---\ndeliverable_id: {sibling_plan_id}\ntitle: sibling\n---\nbody\n",
            encoding="utf-8",
        )
    else:
        (repo / chunk_path).write_text("x = 1\n", encoding="utf-8")
    req = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=(chunk_path,)),),
        deliverable_id=marker_id,
        plan_path=plan_rel,
    )
    (repo / "run.mjs").write_text(render_marker(req) + "\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}},
        repo_root=repo / ".git"
    )
    return out, head


def _assert_refused(repo, out, head, marker_id, plan_id):
    assert out["committed"] is False
    assert out["deliverable_id_marker"] == marker_id
    assert out["deliverable_id_plan"] == plan_id
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not list(repo.rglob("*wave*record*"))


def test_predicates():
    assert valid_deliverable_id(" dlv-abc ") == "dlv-abc"
    assert valid_deliverable_id("feature/x") is None
    assert valid_deliverable_id("dlv-placeholder-replace-with-x") is None
    assert valid_deliverable_id(None) is None
    assert plan_deliverable_id("no frontmatter") is None


def test_branch_shaped_value_refused(repo):
    out, head = _run(repo, "feature/branch", PLAN_ID)
    _assert_refused(repo, out, head, "feature/branch", PLAN_ID)


def test_foreign_dlv_id_refused(repo):
    out, head = _run(repo, "dlv-foreign9", PLAN_ID)
    _assert_refused(repo, out, head, "dlv-foreign9", PLAN_ID)


def test_marker_id_against_plan_with_none_refused(repo):
    out, head = _run(repo, "dlv-marker1", None)
    _assert_refused(repo, out, head, "dlv-marker1", None)


def test_plan_id_against_marker_with_none_refused(repo):
    out, head = _run(repo, None, PLAN_ID)
    _assert_refused(repo, out, head, None, PLAN_ID)


def test_unreadable_plan_refused(repo):
    out, head = _run(repo, PLAN_ID, None, plan_exists=False)
    _assert_refused(repo, out, head, PLAN_ID, None)


def test_matching_pair_commits_with_one_trailer(repo):
    out, _ = _run(repo, PLAN_ID, PLAN_ID)
    assert out["committed"] is True
    body = _git(["log", "-1", "--format=%B"], repo)
    lines = [ln for ln in body.splitlines() if ln.startswith("Deliverable-Id:")]
    assert lines == [f"Deliverable-Id: {PLAN_ID}"]


def test_chunk_writing_a_sibling_plan_stamps_the_executing_plans_id(repo):
    out, _ = _run(
        repo,
        PLAN_ID,
        PLAN_ID,
        chunk_path="docs/sibling-plan.md",
        sibling_plan_id="dlv-sibling9",
    )
    assert out["committed"] is True
    body = _git(["log", "-1", "--format=%B"], repo)
    lines = [ln for ln in body.splitlines() if ln.startswith("Deliverable-Id:")]
    assert lines == [f"Deliverable-Id: {PLAN_ID}"]
