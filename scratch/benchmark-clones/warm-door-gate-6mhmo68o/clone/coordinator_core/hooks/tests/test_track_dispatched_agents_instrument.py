"""
coordinator_core.hooks.tests.test_track_dispatched_agents_instrument --
breadcrumb coverage for the invisible-drop window in `_handler` (line ~469
onward, BEFORE the asyncio.Lock / locked_rmw append).

Context: a 14-day sweep found 146 agent dispatches with NO bookkeeping row --
both the em-session-id.txt back-pointer AND the dispatched-agents.txt row
absent. Five hypotheses (id-namespace mismatch, PowerShell coverage gap,
among them) were eliminated on evidence. The surviving constraint: the op
died somewhere between entering `_handler` and completing the
`to_thread(_setup_dirs_sync, ...)` call that writes the back-pointer.

This file instruments, not fixes, that window: every guard return in it, plus
the two raise sites (`normalize_teammate_agent_id`, `git_common_dir`) and
`asyncio.CancelledError` while queued, now leave a stderr breadcrumb on the
SAME fail-open sink `_process_dispatched_locked`'s LockTimeout leg already
uses -- these tests assert the breadcrumb fires and that no guard's
disposition (fail-closed reject stays a reject; the git_common_dir fallback
still runs; CancelledError still propagates) changed.

House convention (pyproject.toml `pytest-asyncio` note,
`coordinator_core/ops/tests/test_cutover_gate_handler.py`): plain sync test
functions wrapping the async handler in `asyncio.run(...)` -- zero
`async def test_`, zero `pytest.mark.asyncio`.

Module under test: coordinator_core/hooks/track_dispatched_agents.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import coordinator_core.hooks.track_dispatched_agents as tda

VALID_AGENT_ID = "abcdef0123456789"
VALID_SESSION_ID = "em-session-instrument-1"


def _payload(session_id="", agent_id="", model="", subagent_type="") -> dict:
    return {
        "session_id": session_id,
        "dispatched_agent_id": agent_id,
        "dispatched_model": model,
        "subagent_type": subagent_type,
    }


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    # `git_common_dir` (coordinator_core/lifecycle.py) is a pure upward WALK
    # for a `.git` entry -- it never spawns `git` and never checks that the
    # entry is a valid, initialized repository. A bare `.git` directory is
    # sufficient fixture state; no subprocess needed (spawn-ratchet: see
    # coordinator_core/tests/test_no_new_spawning_tests.py).
    (tmp_path / ".git").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Class 1 -- silent guard returns, one test per leg.
# ---------------------------------------------------------------------------

def test_empty_session_id_guard_breadcrumbs_and_writes_nothing(tmp_path, capsys):
    params = _payload(session_id="", agent_id=VALID_AGENT_ID)
    result = asyncio.run(tda._handler(params, repo_root=str(tmp_path)))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=empty_session_id" in stderr
    assert VALID_AGENT_ID in stderr
    assert not (tmp_path / "coordinator-sessions").exists()


def test_empty_agent_id_guard_breadcrumbs_and_writes_nothing(tmp_path, capsys):
    params = _payload(session_id=VALID_SESSION_ID, agent_id="")
    result = asyncio.run(tda._handler(params, repo_root=str(tmp_path)))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=empty_agent_id" in stderr
    assert VALID_SESSION_ID in stderr
    assert not (tmp_path / "coordinator-sessions").exists()


def test_invalid_agent_id_guard_breadcrumbs_and_stays_fail_closed(tmp_path, capsys):
    bad_id = "not-a-valid-id!"
    params = _payload(session_id=VALID_SESSION_ID, agent_id=bad_id)
    result = asyncio.run(tda._handler(params, repo_root=str(tmp_path)))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=invalid_agent_id" in stderr
    assert bad_id in stderr
    assert not (tmp_path / "coordinator-sessions").exists()


def test_empty_repo_root_guard_breadcrumbs_and_writes_nothing(capsys):
    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID)
    result = asyncio.run(tda._handler(params, repo_root=None))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=empty_repo_root" in stderr
    assert VALID_AGENT_ID in stderr


def test_normalize_teammate_agent_id_raise_fails_open(monkeypatch, tmp_path, capsys):
    def _boom(agent_id, live_session_id):
        raise ValueError("synthetic raise for instrument coverage")

    monkeypatch.setattr(
        "coordinator_core.write_guards._subagent_identity.normalize_teammate_agent_id", _boom
    )

    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID)
    result = asyncio.run(tda._handler(params, repo_root=str(tmp_path)))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=normalize_teammate_agent_id_raised" in stderr
    assert "synthetic raise for instrument coverage" in stderr
    assert not (tmp_path / "coordinator-sessions").exists()


def test_git_common_dir_raise_breadcrumbs_but_still_falls_back_and_writes(tmp_path, capsys):
    # tmp_path has no .git anywhere in its ancestry -- git_common_dir raises
    # RuntimeError, which is NOT one of the four early-return guards: the
    # existing fallback (`Path(repo_root) / "coordinator-sessions"`) still
    # runs and the dispatch still gets recorded. Only the breadcrumb is new.
    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID, subagent_type="claude")
    result = asyncio.run(tda._handler(params, repo_root=str(tmp_path)))

    assert result == {}
    stderr = capsys.readouterr().err
    assert "guard=git_common_dir_raised" in stderr

    dispatched = tmp_path / "coordinator-sessions" / VALID_SESSION_ID / "dispatched-agents.txt"
    assert dispatched.exists()
    assert VALID_AGENT_ID in dispatched.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Class 2 -- cancellation while queued, before or during _setup_dirs_sync.
# ---------------------------------------------------------------------------

def test_cancelled_before_setup_dirs_reraises_and_logs_checkpoint(monkeypatch, git_repo, capsys):
    def _slow_setup(*a, **kw):
        time.sleep(0.3)

    monkeypatch.setattr(tda, "_setup_dirs_sync", _slow_setup)
    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID)

    async def _go():
        task = asyncio.ensure_future(tda._handler(params, repo_root=git_repo))
        await asyncio.sleep(0.05)  # let the handler reach the to_thread await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())

    stderr = capsys.readouterr().err
    assert "track_dispatched_agents: cancelled" in stderr
    assert "checkpoint=before_setup_dirs" in stderr
    assert VALID_AGENT_ID in stderr

    # Signature match: cancellation before _setup_dirs_sync completes leaves
    # BOTH artifacts absent, same as the reported 146-dispatch defect.
    assert not (git_repo / "coordinator-sessions").exists()


def test_cancelled_before_lock_acquire_reraises_and_logs_checkpoint(git_repo, capsys):
    # No monkeypatch of _setup_dirs_sync needed -- the contention that
    # suspends the handler at "before_lock_acquire" comes from a second
    # holder of the SAME per-file asyncio.Lock (D6), acquired up front by
    # this test. `_setup_dirs_sync` and `git_common_dir` both run fast, so
    # the handler reaches `async with lock:` still holding checkpoint=
    # "before_lock_acquire" and suspends there until the holder releases it.
    dispatched = str(
        git_repo / ".git" / "coordinator-sessions" / VALID_SESSION_ID / "dispatched-agents.txt"
    )
    lock = tda._get_file_lock(dispatched)
    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID)

    async def _go():
        await lock.acquire()
        try:
            task = asyncio.ensure_future(tda._handler(params, repo_root=git_repo))
            await asyncio.sleep(0.05)  # let the handler reach the lock-contention await
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            lock.release()

    asyncio.run(_go())

    stderr = capsys.readouterr().err
    assert "track_dispatched_agents: cancelled" in stderr
    assert "checkpoint=before_lock_acquire" in stderr
    assert VALID_AGENT_ID in stderr

    # Signature match: cancellation before the lock is even acquired leaves
    # the dispatched-agents.txt row unwritten, same as the reported defect.
    dispatched_path = Path(dispatched)
    assert not dispatched_path.exists()


def test_cancelled_before_process_dispatched_reraises_and_logs_checkpoint(
    monkeypatch, git_repo, capsys
):
    def _slow_process_dispatched(*a, **kw):
        time.sleep(0.3)

    monkeypatch.setattr(tda, "_process_dispatched_locked", _slow_process_dispatched)
    params = _payload(session_id=VALID_SESSION_ID, agent_id=VALID_AGENT_ID)

    async def _go():
        task = asyncio.ensure_future(tda._handler(params, repo_root=git_repo))
        await asyncio.sleep(0.05)  # let the handler reach the to_thread await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())

    stderr = capsys.readouterr().err
    assert "track_dispatched_agents: cancelled" in stderr
    assert "checkpoint=before_process_dispatched" in stderr
    assert VALID_AGENT_ID in stderr

    # Signature match: cancellation here means _setup_dirs_sync already ran
    # (the em-session-id.txt back-pointer IS written) but dispatched-agents.txt
    # never got its row -- the asymmetric-artifact half of the defect
    # signature, distinct from the before_setup_dirs test's both-absent case.
    backpointer = (
        git_repo / ".git" / "coordinator-sessions" / ".agents" / VALID_AGENT_ID / "em-session-id.txt"
    )
    assert backpointer.exists()
    dispatched = (
        git_repo / ".git" / "coordinator-sessions" / VALID_SESSION_ID / "dispatched-agents.txt"
    )
    assert not dispatched.exists()


# ---------------------------------------------------------------------------
# Success path -- byte-unchanged behaviour: no breadcrumb, correct writes.
# ---------------------------------------------------------------------------

def test_success_path_writes_both_artifacts_with_no_breadcrumb(git_repo, capsys):
    params = _payload(
        session_id=VALID_SESSION_ID,
        agent_id=VALID_AGENT_ID,
        model="claude-sonnet-5",
        subagent_type="claude",
    )
    result = asyncio.run(tda._handler(params, repo_root=git_repo))

    assert result == {}
    stderr = capsys.readouterr().err
    assert stderr == ""

    common = git_repo / ".git"
    dispatched = common / "coordinator-sessions" / VALID_SESSION_ID / "dispatched-agents.txt"
    backpointer = (
        common / "coordinator-sessions" / ".agents" / VALID_AGENT_ID / "em-session-id.txt"
    )
    assert dispatched.exists()
    assert VALID_AGENT_ID in dispatched.read_text(encoding="utf-8")
    assert backpointer.read_text(encoding="utf-8").strip() == VALID_SESSION_ID
