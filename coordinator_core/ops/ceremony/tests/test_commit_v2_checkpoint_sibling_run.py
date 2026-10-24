"""A wave checkpoint through `ceremony.commit_v2` lands its declared set only: a sibling stopped
run's artifacts are not carried into it, and a mixed product/process checkpoint that the
checkpoint guard refuses lands neither half."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cks-self"
SCRIPT = "scratch/warp/x.mjs"
PLAN = "state/mise-inventory/run.spine.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _files(repo: Path, rev: str = "HEAD") -> set:
    return set(_git(["show", "--name-only", "--format=", rev], repo).split())


def _message(base: str) -> str:
    return (
        "checkpoint(wave 1): 1 rows — r1\n\n"
        f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"
    )


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
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _stopped_sibling(repo: Path, tmp_path: Path) -> str:
    stamp = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"ask-{stamp}-17c2e0"
    transcript = tmp_path / "t.jsonl"
    launch = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu"}]},
        "toolUseResult": {"status": "async_launched", "taskId": "w1"},
    }
    transcript.write_text(json.dumps(launch) + "\nSuccessfully stopped task: w1\n", encoding="utf-8")
    run_token.mint(repo, run_id, SCRIPT, session_id=SELF, sizing_rel=None)
    warp_live.record_submission(
        repo, run_id, session_id=SELF, transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=None, tool_use_id="tu",
    )
    _write(repo, f"state/warp/{run_id}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))
    _write(repo, f"state/warp/{run_id}/requirements.md")
    _write(repo, "state/sizings/s.yaml", "baton: state/handoffs/b.md\n")
    _write(repo, "state/handoffs/b.md", "---\ncarried_items: []\n---\nbody\n")
    return run_id


def test_checkpoint_does_not_carry_a_stopped_sibling_runs_residue(repo, tmp_path):
    sibling = _stopped_sibling(repo, tmp_path)
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, "tests/test_a.py")

    result = _call(repo, {"paths": ["tests/test_a.py"], "message": _message(base)})

    assert result["committed"] is True, result
    assert "stopped_run_residue" not in result
    assert _files(repo) == {"tests/test_a.py"}
    assert f"Stopped-Run-Residue: {sibling}" not in _git(["log", "-1", "--format=%B"], repo)


def test_ordinary_commit_does_not_carry_the_stopped_run_residue(repo, tmp_path):
    _stopped_sibling(repo, tmp_path)
    _write(repo, "tests/test_a.py")

    result = _call(repo, {"paths": ["tests/test_a.py"], "message": "m"})

    assert result["committed"] is True, result
    assert "stopped_run_residue" not in result
    assert _files(repo) == {"tests/test_a.py"}


def test_mixed_checkpoint_refused_on_its_process_half_lands_nothing(repo):
    _write(repo, "state/notes/n.md", "v1\n")
    _git(["add", "state/notes/n.md"], repo)
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _git(["commit", "-q", "-m", "peer writes the note"], repo)
    _write(repo, "state/notes/n.md", "v2\n")
    _write(repo, "tests/test_a.py")
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(
        repo, {"paths": ["tests/test_a.py", "state/notes/n.md"], "message": _message(base)}
    )

    assert result.get("committed") is not True
    assert "writes changed outside this run" in json.dumps(result)
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
