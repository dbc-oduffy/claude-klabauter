"""The size-stage write guard reaches only the live unsized run's own spawns: a run agent is denied
a product write while an unrelated executor in the same session is allowed."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core import warp_live
from coordinator_core.hooks import nudge_em_code_dispatch as dispatch
from coordinator_core.ops.dispatch_emit import run_token

SESSION = "sess-1"
RUN_ID = "ask-20261016T000000-abcdef"
AGENT_A = "a1b2c3d4e5f60718"
AGENT_B = "b1b2c3d4e5f60718"
AGENT_C = "c1b2c3d4e5f60718"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _session(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """A repo with one live unsized run and its resolved launch record; returns (root, session
    transcript, the run's workflow transcript dir)."""
    monkeypatch.setattr(dispatch, "_is_scratch_or_tmp", lambda _p: False)  # tmp_path is under the temp dir
    launches = tmp_path / "launches"
    launches.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: launches)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda _name="": launches)

    root = _repo(tmp_path)
    script = root / "scratch" / f"{RUN_ID}.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("export const meta = {}\n", encoding="utf-8")
    run_token.mint(root, RUN_ID, str(script), session_id=SESSION)

    session_transcript = tmp_path / f"{SESSION}.jsonl"
    _touch(session_transcript)
    run_dir = tmp_path / SESSION / "subagents" / "workflows" / "wf_run"
    (launches / f"workflow-run-{SESSION}-task1.json").write_text(
        json.dumps({"scriptPath": str(script), "transcript_dir": str(run_dir), "fired_at": 1.0}),
        encoding="utf-8",
    )
    return root, session_transcript, run_dir


def _edit(root: Path, session_transcript: Path, agent_id: str) -> dict | None:
    return dispatch.op({
        "tool_name": "Edit",
        "agent_id": agent_id,
        "session_id": SESSION,
        "cwd": str(root),
        "transcript_path": str(session_transcript),
        "tool_input": {"file_path": str(root / "pkg" / "mod.py"), "old_string": "a", "new_string": "b"},
    })


def test_run_agent_is_denied_and_unrelated_executor_is_allowed(tmp_path, monkeypatch):
    root, session_transcript, run_dir = _session(tmp_path, monkeypatch)
    _touch(run_dir / f"agent-{AGENT_A}.jsonl")
    _touch(tmp_path / SESSION / "subagents" / f"agent-{AGENT_B}.jsonl")
    _touch(tmp_path / SESSION / "subagents" / "workflows" / "wf_other" / f"agent-{AGENT_C}.jsonl")

    denied = _edit(root, session_transcript, AGENT_A)
    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Size stage: change no file but the sizing" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    assert _edit(root, session_transcript, AGENT_B) is None
    assert _edit(root, session_transcript, AGENT_C) is None


def test_run_agent_is_allowed_once_the_run_binds_a_sizing(tmp_path, monkeypatch):
    root, session_transcript, run_dir = _session(tmp_path, monkeypatch)
    _touch(run_dir / f"agent-{AGENT_A}.jsonl")

    assert _edit(root, session_transcript, AGENT_A) is not None
    run_token.bind_sizing(root, RUN_ID, "state/sizings/a.yaml")
    assert _edit(root, session_transcript, AGENT_A) is None
