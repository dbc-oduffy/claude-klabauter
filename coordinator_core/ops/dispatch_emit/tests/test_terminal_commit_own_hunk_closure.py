"""The terminal commit withholds a staged consumer whose definition stayed in a foreign-excluded path.

Case 7 on the terminal path: run B lands `pkg/shared.py` settled (HEAD + B's hunk, P's hunk left in the
worktree), but `pkg/prod.py` is a peer's undeclared dirt that defines `new_helper`; B's `pkg/consumer.py`
calls it, so the consumer leaves with the producer. A consumer of a definition B's own settled hunk adds
still lands: a settled path is not withheld. Known gap: a definition living only in a settled path's
excluded foreign hunk is out of reach of `checkpoint_hunks.consumers_of_withheld_definitions`.
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

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000c2"
X = "pkg/shared.py"
PROD = "pkg/prod.py"
CONS = "pkg/consumer.py"
CONS_OWN = "pkg/consumer_own.py"
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


def _shared(p_edit: bool = False, b_edit: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if p_edit:
        rows[P_LINE - 1] = f"x_{P_LINE} = 'P-EDIT'"
    if b_edit:
        rows[B_LINE - 1] = "def own_helper():\n    return 'b'"
    return "\n".join(rows) + "\n"


def _declare(root: Path, run_id: str, writes: tuple, baseline: dict) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=writes, wave=0,
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
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / X).write_text(_shared(), encoding="utf-8")
    (root / PROD).write_text("def old():\n    return 1\n", encoding="utf-8")
    (root / CONS).write_text("x = 1\n", encoding="utf-8")
    (root / CONS_OWN).write_text("y = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    baseline = wb.capture(root, [X, CONS, CONS_OWN])
    _declare(root, RUN_P, (X,), baseline)
    _declare(root, RUN_B, (X, CONS, CONS_OWN), baseline)
    run_token.mint(root, RUN_P, "run-p.mjs", session_id=PEER_SESSION)
    run_token.mint(root, RUN_B, "run.mjs", session_id=SELF)
    (root / X).write_text(_shared(p_edit=True, b_edit=True), encoding="utf-8")
    (root / PROD).write_text("def old():\n    return 1\n\n\ndef new_helper():\n    return 2\n", encoding="utf-8")
    (root / CONS).write_text("from pkg.prod import new_helper\n\nvalue = new_helper()\n", encoding="utf-8")
    (root / CONS_OWN).write_text("from pkg.shared import own_helper\n\nvalue = own_helper()\n", encoding="utf-8")
    return root


def _fire_b(root: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(X, PROD, CONS, CONS_OWN)),))
    marker = root / RUN_DIR_ROOT / RUN_B / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN_B}/manifest.json\n", encoding="utf-8")
    params = {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN_B,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_consumer_of_a_foreign_excluded_definition_is_withheld_and_own_hunk_consumer_lands(repo):
    seed = _git(["rev-parse", "HEAD"], repo)

    out = _fire_b(repo)

    assert out.get("committed") is True, out
    assert CONS not in (out.get("residue") or {}), out
    assert "still dirty" not in str(out.get("error")), out
    landed = _git(["diff", "--name-only", seed, "HEAD"], repo).splitlines()
    assert X in landed
    assert CONS not in landed and PROD not in landed
    assert CONS_OWN in landed
    assert "own_helper" in _git(["show", f"HEAD:{X}"], repo)
    assert "consumer of a withheld producer" in out["own_hunk_closure_withheld"][CONS]
    assert CONS_OWN not in out["own_hunk_closure_withheld"]
    assert "new_helper" in (repo / CONS).read_text(encoding="utf-8")
