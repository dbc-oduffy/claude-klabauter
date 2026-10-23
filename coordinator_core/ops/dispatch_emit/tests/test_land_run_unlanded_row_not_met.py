"""dispatch.land_run never replies or records criterion `met` while a credited row is unlanded."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000b2"
RUN_A = "plan-20261021T100000-bbbbbb"
RUN_B = "run-b"
SCRIPT = "run-a.mjs"
MET = {"status": "met", "observation": "obs", "sidecar": "j.md"}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _claim(root: Path, rel: str) -> None:
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _land(repo: Path, *, peer_holds_c2: bool) -> dict:
    _write(repo, "src/a.py", "a = 1\n")
    _write(repo, "src/b.py", "b = 1\n")
    for rel in ("src/a.py", "src/b.py"):
        _claim(repo, rel)
    if peer_holds_c2:
        _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "X1", "writes": ["src/b.py"]}]}))
        run_token.mint(repo, RUN_B, "b.mjs", session_id=SELF)
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN_A}/run.json", json.dumps({"run_id": RUN_A}))
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("src/a.py",)),
            ChunkCommit(id="C2", title="t", paths=("src/b.py",)),
        ),
        plan_path=None,
    )
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [],
         "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0, "criterion": dict(MET)},
         "session_id": SELF, "run_id": RUN_A},
        repo_root=repo / ".git",
    )


def test_an_unlanded_credited_row_demotes_a_met_judge_to_not_met(repo):
    out = _land(repo, peer_holds_c2=True)

    assert out["committed"] is True, out
    assert "C2" in out["incomplete_reasons"]
    assert out["criterion_status"] == "not_met"


def test_with_every_row_landed_a_met_judge_is_still_met(repo):
    out = _land(repo, peer_holds_c2=False)

    assert out["committed"] is True, out
    assert not out.get("incomplete_reasons")
    assert out["criterion_status"] == "met"
