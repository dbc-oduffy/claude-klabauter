"""land_run withholds a review-only marker's review_carried paths from the landing; the same marker
without the field lands the dirty path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.landing_remedy import REVIEW_CARRIED_REASON
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c5"
RUN = "plan-20261022T100000-c5c5c5"
SCRIPT = "run-c5.mjs"
CARRIED = "a.py"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, CARRIED, "a = 1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _land(repo: Path, carried: tuple) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, CARRIED, "a = 2\n")
    session_scope.touch(SELF, CARRIED, cwd=str(repo))
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=CARRIED,
        content_hash=touch_record.compute_content_hash(repo / CARRIED), kind="w",
    )
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(CARRIED,)),), plan_path=None,
        review_only=True, review_carried=carried,
    )
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN, "run_base_sha": base},
        repo_root=repo / ".git",
    )


def test_review_carried_path_is_withheld_from_the_landing(repo):
    out = _land(repo, (CARRIED,))

    assert CARRIED not in _git(["show", "--name-only", "--format=", "HEAD"], repo).split(), out
    assert CARRIED in {ln[3:] for ln in _git(["status", "--porcelain"], repo).splitlines()}
    body = _git(["log", "-1", "--format=%B"], repo)
    assert f"{CARRIED} {REVIEW_CARRIED_REASON}" in body, (body, out)


def test_control_same_marker_without_the_field_lands_the_path(repo):
    out = _land(repo, ())

    assert out.get("committed") is True, out
    assert CARRIED in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
