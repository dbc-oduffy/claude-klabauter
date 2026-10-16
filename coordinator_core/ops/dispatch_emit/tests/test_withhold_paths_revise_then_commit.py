"""End to end: workflow.revise withhold_paths, then dispatch.terminal_commit leaves the path out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261016T120000-cccccc"
REASON = "peer run hunks"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "e2e-self")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def test_revise_withhold_then_terminal_commit_leaves_the_path_out(repo):
    rl.start(repo, RUN)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "shared.tsx").write_text("shared\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=("shared.tsx",)),
        )
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")

    revised = rl._handler(
        {"run_id": RUN, "withhold_paths": [{"path": "shared.tsx", "reason": REASON}]},
        repo_root=repo,
    )
    assert revised["ok"] is True, revised
    assert revised["withhold_paths"] == [{"path": "shared.tsx", "reason": REASON}]

    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
            "run_id": RUN,
        },
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    landed = set(_git(["log", "--name-only", "--format="], repo).split())
    assert "a.py" in landed
    assert "shared.tsx" not in landed
    assert "?? shared.tsx" in _git(["status", "--porcelain"], repo).splitlines()
    assert out["run_artifacts"]["withheld"]["shared.tsx"] == f"withhold_paths: {REASON}"
    assert out["withheld_chunks"] == {"C2": [{"path": "shared.tsx", "reason": REASON}]}
    assert "C2" not in out["chunks_committed"]
    assert "C2" not in out.get("no_product_hunk", [])
    assert "C1" in out["chunks_committed"]
