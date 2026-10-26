"""A resumed landing reads the halted run's review writes: its start time and its emitter session."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, terminal_commit
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUMED_RUN_ID_KEY,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000021"
EMITTER = "5e1f0000-0000-4000-8000-000000000022"
HALTED = "run-halted"
RESUMED = "run-resumed"
HALTED_STARTED = 1000.0
RESUMED_STARTED = 5000.0
_REVIEWED = {**reviewed(), "fixes": 1}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root / "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _write(root / "src" / "a.py", "a\n")
    _write(root / RUN_DIR_ROOT / HALTED / "submission.json", json.dumps({"submitted_at": HALTED_STARTED}))
    _write(root / RUN_DIR_ROOT / RESUMED / "submission.json", json.dumps({"submitted_at": RESUMED_STARTED}))
    _write(
        root / RUN_DIR_ROOT / f"{HALTED}.workflow.mjs.emitted.json", json.dumps({"session_id": EMITTER})
    )
    (root / RECORD_DIR_ROOT / HALTED).mkdir(parents=True)
    return root


def _capture(monkeypatch) -> list:
    calls: list = []

    def fake(root, sid, since):
        calls.append((sid, since))
        return {}

    monkeypatch.setattr(review_edits, "review_writes", fake)
    return calls


def _run(repo: Path, inline_review: dict) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    _write(repo / "run.mjs", "// emitted\n" + render_marker(request) + "\n")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": inline_review,
            "session_id": SELF, "run_id": RESUMED,
        },
        repo_root=repo / ".git",
    )


def test_resumed_run_id_reads_the_halted_runs_start_and_emitter(repo, monkeypatch):
    calls = _capture(monkeypatch)
    _run(repo, {**_REVIEWED, RESUMED_RUN_ID_KEY: HALTED})
    assert calls == [(EMITTER, HALTED_STARTED)]


def test_without_the_key_the_edit_window_is_the_current_runs(repo, monkeypatch):
    calls = _capture(monkeypatch)
    _run(repo, dict(_REVIEWED))
    assert calls == [(SELF, RESUMED_STARTED)]


def test_an_unknown_or_unsafe_resumed_run_id_falls_back(repo):
    for bad in ("run-missing", "../escape", "", 7):
        assert terminal_commit._resumed_review_source(
            repo, {RESUMED_RUN_ID_KEY: bad}, RESUMED, SELF
        ) == (RESUMED, SELF)


def test_a_halted_run_without_a_receipt_keeps_the_callers_session(repo):
    (repo / RUN_DIR_ROOT / f"{HALTED}.workflow.mjs.emitted.json").unlink()
    assert terminal_commit._resumed_review_source(
        repo, {RESUMED_RUN_ID_KEY: HALTED}, RESUMED, SELF
    ) == (HALTED, SELF)
