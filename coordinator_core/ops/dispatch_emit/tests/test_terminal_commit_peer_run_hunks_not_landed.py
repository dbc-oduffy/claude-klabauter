"""A run's terminal commit never lands a live peer run's hunks of a shared file.

Runs B and P both declare X and both recorded their ``write_baseline`` at the same seed. The worktree
carries P's edit and B's edit in disjoint regions. B's commit lands B's hunk only, or refuses naming
run P; it never whole-file stages P's hunk into HEAD.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MEMO = "2026-11-06-example-game-repo-em-warp-commit-landed-peer-held-hunks.md"
SELF = "5e1f0000-0000-4000-8000-0000000000e1"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000e2"
X = "pkg/shared.py"
LINES = 120
RUN_P = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 600))}-aaaaaa"
RUN_B = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
P_LINE = 5
B_LINE = 115


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _body(p_edit: bool = False, b_edit: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if p_edit:
        rows[P_LINE - 1] = f"x_{P_LINE} = 'P-EDIT'"
    if b_edit:
        rows[B_LINE - 1] = f"x_{B_LINE} = 'B-EDIT'"
    return "\n".join(rows) + "\n"


def _declare(root: Path, run_id: str, baseline: dict) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=(X,), wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
        write_baseline=baseline,
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    (root / X).write_text(_body(), encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    baseline = wb.capture(root, [X])
    _declare(root, RUN_P, baseline)
    _declare(root, RUN_B, baseline)
    run_token.mint(root, RUN_P, "run-p.mjs", session_id=PEER_SESSION)
    run_token.mint(root, RUN_B, "run.mjs", session_id=SELF)
    (root / X).write_text(_body(p_edit=True, b_edit=True), encoding="utf-8")
    return root


def _fire_b(root: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(X,)),))
    marker = root / RUN_DIR_ROOT / RUN_B / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN_B}/manifest.json\n", encoding="utf-8")
    params = {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN_B,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


@pytest.mark.xfail(strict=True, reason=_MEMO)
def test_b_commit_never_lands_a_live_peer_runs_hunk_of_the_shared_file(repo):
    seed = _git(["rev-parse", "HEAD"], repo)

    out = _fire_b(repo)

    if out.get("committed") is True:
        landed = _git(["show", f"HEAD:{X}"], repo)
        assert "P-EDIT" not in landed, "the peer run's hunk was whole-file staged into HEAD"
        assert "x_115 = 'B-EDIT'" in landed
    else:
        assert RUN_P in str(out.get("error")), out
        assert _git(["rev-parse", "HEAD"], repo) == seed
    assert "P-EDIT" in (repo / X).read_text(encoding="utf-8")


def test_b_commit_without_a_peer_run_lands_its_own_hunk(repo):
    (repo / "state" / "warp" / RUN_P / "manifest.json").unlink()

    out = _fire_b(repo)

    assert out["committed"] is True, out
    assert "x_115 = 'B-EDIT'" in _git(["show", f"HEAD:{X}"], repo)
