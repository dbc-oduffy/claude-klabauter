"""dispatch.terminal_commit carries the sizing its plan cites as `sizing_object:`.

The warp ask arm rewrites the sizing during the run (status routed, plan link);
no chunk declares it, so without the ride-along it stayed dirty after the commit.
"""

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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
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
    assert _SIZING in _git(["show", "--name-only", "--format=", out["sha"]], repo)
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
