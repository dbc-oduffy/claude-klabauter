"""A codex EM's `engine_ask.submit` (the headless-EM door, `submit_ask`) on the real emit: a run-record write
its sandbox denies raises at once instead of spinning into a stall refusal, so the run still mints; a denial
that does surface is refused as that denial; a slow write still refuses as a stall."""

from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks._engine_ask_gates import submit_ask
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import run_token

repo = real.repo
DENIED = "Access is denied"
STALL = "the disk is stalled"


@pytest.fixture(autouse=True)
def _codex_headless_em(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "0c8eeac3-0000-0000-0000-000000000000")
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "codex")
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    from coordinator_core.ops.dispatch_emit import op

    monkeypatch.setattr(op, "run_bounded", lambda key, fn, **kw: fn())  # the 0.2s record-dir bound trips on a loaded box


@pytest.fixture
def release():
    event = threading.Event()
    yield event
    event.set()


def _submit(repo_root, evaluate=None) -> dict:
    return submit_ask(
        ask="build the thing", ask_source="pm", session_id="sess-codex-em", caller="codex-em",
        cwd=str(repo_root), evaluate=evaluate,
    )


def _token_dir(repo_root):
    return run_token.engine_record_dir(repo_root) / run_token._DIR


def _deny_creates(monkeypatch, release: threading.Event) -> None:
    """The sandbox refuses a create under the record dir. `mkstemp` models Windows, where it retries that
    refusal as a name collision (minutes of CPU) instead of raising."""
    real_open, real_mkstemp = os.open, tempfile.mkstemp

    def open_(path, *args, **kwargs):
        if ".mint-probe." in str(path):
            raise PermissionError(13, DENIED)
        return real_open(path, *args, **kwargs)

    def spinning_mkstemp(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith(".mint."):
            release.wait(30)
            raise PermissionError(13, DENIED)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(run_token.os, "open", open_)
    monkeypatch.setattr(run_token.tempfile, "mkstemp", spinning_mkstemp)


@pytest.mark.deliberate_wall_clock(reason="a regression back to the spinning create burns the hook budget before it fails")
def test_a_sandbox_denied_record_write_neither_stalls_nor_blocks_the_mint(repo, monkeypatch, release):
    _deny_creates(monkeypatch, release)
    started = time.monotonic()
    reply = _submit(repo)
    assert time.monotonic() - started < 1.5
    assert "refused" not in reply and STALL not in str(reply), reply
    assert reply["run_id"] and reply["script_path"]
    assert not list(_token_dir(repo).glob("ask-*.json"))  # no token record: the run hands its commit back


@pytest.mark.deliberate_wall_clock(reason="the sandbox denial surfaces after the emit begins its write; the follow-up scans run to the deadline")
def test_a_denial_that_reaches_the_hook_is_refused_as_the_denial_with_its_remedy(repo):
    def emit(params, root):
        run_token.WRITE_GATE.get().begin()
        raise PermissionError(13, DENIED)

    reply = _submit(repo, evaluate=lambda payload: mod.evaluate(payload, emit=emit))
    assert reply["refused"] == "gate-refused"
    assert DENIED in reply["reason"] and ".git" in reply["reason"]
    assert STALL not in reply["reason"]


@pytest.mark.deliberate_wall_clock(reason="runs the hook to its emit budget against a blocked create")
def test_a_slow_record_write_that_never_raises_still_refuses_as_a_stall(repo, monkeypatch, release):
    real_mkstemp = tempfile.mkstemp

    def stalled(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith(".mint."):
            release.wait(30)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(run_token.tempfile, "mkstemp", stalled)
    reply = _submit(repo)
    assert reply["refused"] == "gate-refused"
    assert STALL in reply["reason"]


def test_an_allowed_record_write_mints_the_run_and_its_token(repo):
    reply = _submit(repo)
    assert reply["run_id"], reply
    assert list(_token_dir(repo).glob("ask-*.json"))
    assert not list(_token_dir(repo).glob(".mint*"))
