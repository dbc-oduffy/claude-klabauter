"""plan_trailer_deliverable_ids and the extra Deliverable-Id trailers terminal_commit appends."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    plan_trailer_deliverable_ids,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_ID = "dlv-plan111"


def _plan(extra: str, own: str = PLAN_ID) -> str:
    return f"---\ndeliverable_id: {own}\n{extra}title: t\n---\nbody\n"


def test_absent_or_empty_list():
    assert plan_trailer_deliverable_ids(_plan("")) == []
    assert plan_trailer_deliverable_ids(_plan("trailer_deliverable_ids: []\n")) == []
    assert plan_trailer_deliverable_ids("no frontmatter") == []
    assert plan_trailer_deliverable_ids(_plan("trailer_deliverable_ids: dlv-x\n")) == []


def test_one_and_two_ids():
    assert plan_trailer_deliverable_ids(_plan('trailer_deliverable_ids: ["dlv-a"]\n')) == ["dlv-a"]
    assert plan_trailer_deliverable_ids(_plan("trailer_deliverable_ids: [dlv-a, dlv-b]\n")) == ["dlv-a", "dlv-b"]


def test_invalid_dedup_and_own_dropped():
    got = plan_trailer_deliverable_ids(
        _plan(
            "trailer_deliverable_ids: [dlv-a, dlv-a, bogus, 7, dlv-placeholder-replace-with-x, dlv-plan111]\n"
        )
    )
    assert got == ["dlv-a"]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("extras,expected", [([], []), (["dlv-a"], ["dlv-a"]), (["dlv-a", "dlv-b", "bad"], ["dlv-a", "dlv-b"])])
def test_terminal_commit_trailer_block(tmp_path, extras, expected):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "docs").mkdir()
    ids = "".join(f"  - {e}\n" for e in extras)
    block = f"trailer_deliverable_ids:\n{ids}" if extras else ""
    (root / "docs/plan.md").write_text(_plan(block), encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    req = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),),
        deliverable_id=PLAN_ID,
        plan_path="docs/plan.md",
    )
    (root / "run.mjs").write_text(render_marker(req) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [],
         "inline_review": {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}},
        repo_root=root / ".git",
    )
    assert out.get("committed"), out
    parsed = _git(["log", "-1", "--format=%(trailers:key=Deliverable-Id,valueonly)"], root).split()
    assert parsed == [PLAN_ID, *expected]
    inline = _git(["log", "-1", "--format=%(trailers:key=Inline-Review,valueonly)"], root)
    assert "rev-stem" in inline
