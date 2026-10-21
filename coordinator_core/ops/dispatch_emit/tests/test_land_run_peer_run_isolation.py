"""Two runs share one tree: run A's landing never reads, stages or names run B's dirt, and no foreign
commit on B's file turns A's landing into a refusal."""

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

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
REVIEW_AGENT = "review-agent-1"
RUN_A = "plan-20261021T100000-aaaaaa"
RUN_B = "run-b"
SCRIPT = "run-a.mjs"
PLAN_B = "docs/plans/run-b-plan.md"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _record(root: Path, rel: str, agent_id=None) -> None:
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=agent_id, verb="T", path=rel,
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
    _write(root, PLAN_B, "one\n")
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _tree(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def test_run_a_lands_its_own_paths_while_run_bs_dirt_stays_unread_and_unnamed(repo):
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, PLAN_B, "one\nforeign\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "foreign commit adds lines to run B's plan"], repo)
    foreign_blob = _git(["rev-parse", f"HEAD:{PLAN_B}"], repo).strip()
    _write(repo, PLAN_B, "one\nforeign\nrun B is still editing\n")
    _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": [PLAN_B]}]}))
    run_token.mint(repo, RUN_B, "b.mjs", session_id=SELF)

    for rel, agent in (("src/a.py", None), ("src/claimed.py", None), ("src/review_written.py", REVIEW_AGENT)):
        _write(repo, rel, f"{Path(rel).stem} = 1\n")
        session_scope.touch(SELF, rel, cwd=str(repo))
        _record(repo, rel, agent)
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN_A}/run.json", json.dumps({"run_id": RUN_A}))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("src/a.py",)),), plan_path=None)
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")

    out = land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN_A, "run_base_sha": base},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert _tree(repo) == sorted(["src/a.py", f"{RECORD_DIR_ROOT}/{RUN_A}/run.json"])
    assert _git(["rev-parse", f"HEAD:{PLAN_B}"], repo).strip() == foreign_blob
    assert {PLAN_B, "src/claimed.py", "src/review_written.py"} <= _dirty(repo)
    reply = json.dumps(out)
    assert "run-b-plan" not in reply and "claimed.py" not in reply and "review_written.py" not in reply
    assert out["stranded"] == {}
    assert "run_owned" not in out and "undeclared_dirty" not in out


def test_control_a_declared_path_run_b_also_declares_is_held_not_swept(repo):
    _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": ["src/shared.py"]}]}))
    run_token.mint(repo, RUN_B, "b.mjs", session_id=SELF)
    _write(repo, "src/shared.py", "shared = 1\n")
    session_scope.touch(SELF, "src/shared.py", cwd=str(repo))
    _record(repo, "src/shared.py")
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN_A}/run.json", json.dumps({"run_id": RUN_A}))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("src/shared.py",)),), plan_path=None)
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")

    out = land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN_A},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert "src/shared.py" not in _tree(repo)
    assert out["stranded"] == {"C1": ["src/shared.py"]}
