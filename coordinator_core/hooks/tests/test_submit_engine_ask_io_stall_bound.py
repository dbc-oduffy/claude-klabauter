"""The submit hook's record-write legs are bounded by the door deadline: a blocked write detaches
and the envelope names it, a raised write keeps its existing failure path."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import run_token

WINDOW_S = 0.2
BOUND_S = 3.0


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _payload(root: Path, args) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-stall",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _emit(params: dict, root: Path) -> dict:
    run_id = "ask-20261103T000000-abcdef"
    script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("export const meta = {}\n", encoding="utf-8")
    return {"ok": True, "path": str(script), "run_id": run_id, "fire_args": {"repoRoot": "/r"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)
    monkeypatch.setattr(mod, "_emit_join_s", lambda: WINDOW_S)


@pytest.fixture
def release():
    event = threading.Event()
    yield event
    event.set()


def _blocker(release: threading.Event):
    def block(*args, **kwargs):
        release.wait(30)
        raise OSError("released")

    return block


def test_blocked_mint_detaches_the_resume_token_within_the_window(tmp_path, monkeypatch, release):
    monkeypatch.setattr(run_token, "mint", _blocker(release))
    started = time.monotonic()
    out = mod._resume_fire_args(_repo(tmp_path), "ask-20261103T000000-abcdef", "state/sizings/x.yaml", "s")
    assert time.monotonic() - started < BOUND_S
    assert out == {"detached": ["run_token.mint"]}


def test_raised_mint_keeps_the_mint_failure_path(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    for exc in (OSError("disk"), ValueError("no git dir")):
        monkeypatch.setattr(run_token, "mint", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        assert mod._resume_fire_args(root, "ask-20261103T000000-abcdef", "state/sizings/x.yaml", "s") == {}


def test_landed_mint_hands_the_token_over(tmp_path, monkeypatch):
    monkeypatch.setattr(run_token, "mint", lambda *a, **k: "tok")
    root = _repo(tmp_path)
    out = mod._resume_fire_args(root, "ask-20261103T000000-abcdef", "state/sizings/x.yaml", "s")
    assert out == {"fire_args": {"repoRoot": root.as_posix(), "runToken": "tok"}}


def test_blocked_live_record_write_answers_an_allow_naming_the_detached_write(tmp_path, monkeypatch, release):
    monkeypatch.setattr(warp_live, "record_submission", _blocker(release))
    root = _repo(tmp_path)
    started = time.monotonic()
    env = mod.evaluate(_payload(root, {"ask": "do x"}), emit=_emit)
    assert time.monotonic() - started < BOUND_S
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["detached"] is True
    assert hso["detachedSteps"] == ["warp_live.record_submission"]
    assert "Detached: warp_live.record_submission" in hso["additionalContext"]
    assert "unknown" in hso["additionalContext"]


def test_raised_live_record_write_is_not_reported_detached(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(warp_live, "record_submission", fail)
    env = mod.evaluate(_payload(_repo(tmp_path), {"ask": "do x"}), emit=_emit)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "detached" not in hso


def test_default_entry_with_mint_blocked_forever_answers_inside_the_bound_without_claiming_a_run(
    tmp_path, monkeypatch, release
):
    monkeypatch.setattr(run_token, "mint", _blocker(release))
    started = time.monotonic()
    env = mod.evaluate(_payload(_repo(tmp_path), {"ask": "do x"}))
    assert time.monotonic() - started < BOUND_S
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "updatedInput" not in hso
