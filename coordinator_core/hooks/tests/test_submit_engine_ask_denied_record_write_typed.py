"""A record write the OS denied is refused naming the denied path and the principal that lacks write,
and neither points the operator at the repo's `.git` nor advises a grant."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks._engine_ask_gates import submit_ask
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import run_token

repo = real.repo
DENIED = "Access is denied"
PRINCIPAL = "CodexSandboxUsers"


@pytest.fixture(autouse=True)
def _principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_sandbox_principal_names", lambda: PRINCIPAL)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")


def _submit(repo_root, evaluate) -> dict:
    return submit_ask(
        ask="build the thing", ask_source="pm", session_id="sess-denied-typed", caller="codex-em",
        cwd=str(repo_root), evaluate=evaluate,
    )


def test_a_denial_raised_from_the_emit_names_the_denied_path_and_principal(repo, tmp_path) -> None:
    target = str(tmp_path / "fixture" / "scratch")

    def emit(params, root):
        run_token.WRITE_GATE.get().begin()
        raise PermissionError(13, DENIED, target)

    reply = _submit(repo, lambda payload: mod.evaluate(payload, emit=emit))
    assert reply["refused"] == "gate-refused"
    assert target in reply["reason"] and PRINCIPAL in reply["reason"] and DENIED in reply["reason"]
    assert ".git" not in reply["reason"]
    assert "grant" not in reply["reason"] and "create that directory" in reply["reason"]


def test_a_denial_that_escapes_the_gates_names_the_denied_path_and_principal(repo, tmp_path) -> None:
    target = str(tmp_path / "fixture" / "scratch")

    def emit(params, root):
        raise PermissionError(13, DENIED, target)

    reply = _submit(repo, lambda payload: mod.evaluate(payload, emit=emit))
    assert reply["refused"] == "gate-refused"
    assert target in reply["reason"] and PRINCIPAL in reply["reason"]
    assert ".git" not in reply["reason"] and "grant" not in reply["reason"]


def test_a_denial_without_a_filename_still_names_the_principal(repo) -> None:
    def emit(params, root):
        run_token.WRITE_GATE.get().begin()
        raise PermissionError(13, DENIED)

    reply = _submit(repo, lambda payload: mod.evaluate(payload, emit=emit))
    assert "<unknown>" in reply["reason"] and PRINCIPAL in reply["reason"]


def test_the_principal_falls_back_to_the_calling_process_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(mod.os, "name", "posix")
    assert mod._sandbox_principal_names() == "the calling process"
    assert "the calling process" in mod._denied_record_write_text(DENIED, "/x/scratch")
