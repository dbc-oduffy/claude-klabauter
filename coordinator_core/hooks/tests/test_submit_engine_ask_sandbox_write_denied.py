"""A record write the sandbox denies is refused as that cause, never as a disk stall; a codex caller's
Instead line names the engine_ask.submit route, a Claude caller's keeps Workflow(...)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import run_token

DENIED = "Access is denied"
STALL = "the disk is stalled"
CODEX_ROUTE = "coordinator-invoke engine_ask.submit"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _payload(root: Path) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": "do x"}},
        "session_id": "sess-denied",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _reason(env: dict) -> str:
    hso = env["hookSpecificOutput"]
    return hso.get("permissionDecisionReason", "") + hso.get("additionalContext", "")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "COORDINATOR_AGENT_VENDOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)


def _denied_write_then_slow_followup(monkeypatch, release: threading.Event):
    """The emit opens its record write (gate "writing") and the sandbox denies it; the fallback reads
    after the failure then run past the answer deadline."""

    def emit(params, root):
        run_token.WRITE_GATE.get().begin()
        raise PermissionError(13, DENIED)

    calls: list = []
    original = mod._minted_since_start

    def slow_first(call):
        calls.append(1)
        if len(calls) == 1:
            release.wait(30)
        return original(call)

    monkeypatch.setattr(mod, "_minted_since_start", slow_first)
    return emit


@pytest.fixture
def release():
    event = threading.Event()
    yield event
    event.set()


@pytest.mark.deliberate_wall_clock(reason="runs the hook to its answer deadline against a blocked follow-up; blocked time burns no CPU")
def test_denied_record_write_at_the_deadline_is_refused_as_the_denial_not_a_stall(tmp_path, monkeypatch, release):
    emit = _denied_write_then_slow_followup(monkeypatch, release)
    env = mod.evaluate(_payload(_repo(tmp_path)), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    text = _reason(env)
    assert DENIED in text
    assert STALL not in text


@pytest.mark.deliberate_wall_clock(reason="runs the hook to its answer deadline against a blocked mint; blocked time burns no CPU")
def test_a_genuinely_slow_record_write_still_reports_the_stall(tmp_path, monkeypatch, release):
    def emit(params, root):
        run_token.WRITE_GATE.get().begin()
        release.wait(30)
        raise OSError("released")

    env = mod.evaluate(_payload(_repo(tmp_path)), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert STALL in _reason(env)


@pytest.mark.deliberate_wall_clock(reason="runs the hook to its answer deadline against a blocked follow-up; blocked time burns no CPU")
def test_codex_refusal_names_the_engine_ask_route_and_claude_keeps_workflow(tmp_path, monkeypatch, release):
    emit = _denied_write_then_slow_followup(monkeypatch, release)
    monkeypatch.setenv("CODEX_THREAD_ID", "0c8eeac3-0000-0000-0000-000000000000")
    text = _reason(mod.evaluate(_payload(_repo(tmp_path)), emit=emit))
    assert CODEX_ROUTE in text
    assert "Workflow(" not in text

    monkeypatch.delenv("CODEX_THREAD_ID")
    release.set()
    mod_text = mod._refuse("x")["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Instead: `Workflow(" in mod_text
    assert CODEX_ROUTE not in mod_text


def test_codex_refusal_text_when_the_vendor_is_codex(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "codex")
    assert CODEX_ROUTE in mod._refuse("x")["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_sandboxed_write_that_lands_within_budget_mints_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "0c8eeac3-0000-0000-0000-000000000000")
    root = _repo(tmp_path)

    def emit(params, repo_root):
        script = repo_root / "scratch" / "warp" / "ask-20261103T000000-abcdef.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("export const meta = {}\n", encoding="utf-8")
        return {"ok": True, "path": str(script), "run_id": "ask-20261103T000000-abcdef", "fire_args": {"repoRoot": "/r"}}

    started = time.monotonic()
    env = mod.evaluate(_payload(root), emit=emit)
    assert time.monotonic() - started < 5.0
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
