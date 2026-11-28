"""A raw ask whose first submission minted a run that never launched (a sibling PreToolUse hook denied the
Workflow call) is resubmitted: `hooks.submit_engine_ask.evaluate` answers with that run, not a second one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_compose, run_token

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_ASK = "add the widget"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _payload(root: Path, ask: str = _ASK, session: str = "sess-deny-a") -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": ask, "source": "relayed"}},
        "session_id": session,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _run_id(envelope: dict) -> str:
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", envelope
    return re.search(r"run (ask-\w+-\w+)", hso["permissionDecisionReason"]).group(1)


def _minted(root: Path) -> set[str]:
    return {r["run_id"] for r in run_token.held_records(root)}


def test_resubmitted_ask_reuses_the_unlaunched_minted_run(repo: Path) -> None:
    first = _run_id(mod.evaluate(_payload(repo)))
    assert _minted(repo) == {first}

    second = _run_id(mod.evaluate(_payload(repo)))

    assert second == first
    assert _minted(repo) == {first}


def test_a_different_ask_mints_a_fresh_run(repo: Path) -> None:
    first = _run_id(mod.evaluate(_payload(repo)))

    other = _run_id(mod.evaluate(_payload(repo, ask="remove the widget")))

    assert other != first
    assert _minted(repo) == {first, other}


def test_a_different_session_mints_a_fresh_run(repo: Path) -> None:
    first = _run_id(mod.evaluate(_payload(repo)))

    other = _run_id(mod.evaluate(_payload(repo, session="sess-deny-b")))

    assert other != first
    assert _minted(repo) == {first, other}
