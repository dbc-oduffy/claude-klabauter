"""The warm idle tick lands at most one due land-resume entry per tick, off the tick thread, via the
`dispatch.land_run` pool dispatch path, and closes or re-queues the entry from the reply."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from coordinator_core.ops.dispatch_emit import land_resume
from coordinator_core.warm import breadcrumb, idle, lifecycle, server


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "served"
    path.mkdir()
    return path


@pytest.fixture
def ctx(tmp_path, repo, monkeypatch):
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda root=None: tmp_path / "svc")
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [repo])
    yield server._ServerContext(
        name="pipe-resume", sid="sid-resume", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


def _params(run_id: str) -> dict:
    return {"run_id": run_id, "run_token": "tok", "script_path": "scratch/s.json", "incomplete_chunks": []}


def _queue(repo, run_id: str, *, repo_root: bool = True) -> dict:
    params = _params(run_id)
    if repo_root:
        params["repo_root"] = str(repo)
    assert land_resume.enqueue(None, run_id, params, "land_run.commit")
    return params


def _drain_thread(ctx) -> None:
    thread = ctx.__dict__.get("_land_resume_thread")
    if thread is not None:
        thread.join(10)
        assert not thread.is_alive()


def _entry(run_id: str, suffix: str) -> dict:
    return json.loads((land_resume.queue_dir(None) / (run_id + suffix)).read_text(encoding="utf-8"))


def _recorder(ctx, monkeypatch, reply):
    calls: list = []

    def fake(msg, **kwargs):
        calls.append((msg, threading.current_thread()))
        return reply(msg) if callable(reply) else reply

    monkeypatch.setattr(ctx, "_pool_dispatch", fake)
    return calls


def test_due_entry_lands_through_land_run_dispatch_and_finishes(ctx, repo, monkeypatch):
    params = _queue(repo, "run-a")
    calls = _recorder(ctx, monkeypatch, {"jsonrpc": "2.0", "id": 1, "result": {"committed": True, "sha": "abc"}})
    ctx._idle_tick()
    _drain_thread(ctx)
    assert len(calls) == 1
    msg, thread = calls[0]
    assert msg["method"] == "dispatch.land_run"
    assert msg["params"] == params
    assert msg["_origin_worktree"] == str(repo)
    assert thread is not threading.current_thread()
    done = _entry("run-a", land_resume.DONE_SUFFIX)
    assert done["outcome"] == "landed"
    ctx._idle_tick()
    _drain_thread(ctx)
    assert len(calls) == 1


def test_already_landed_reply_finishes_as_landed_never_landed_twice(ctx, repo, monkeypatch):
    _queue(repo, "run-b")
    calls = _recorder(
        ctx, monkeypatch, {"result": {"committed": True, "sha": "abc", "already_landed": True}}
    )
    ctx._idle_tick()
    _drain_thread(ctx)
    assert _entry("run-b", land_resume.DONE_SUFFIX)["outcome"] == "landed"
    ctx._idle_tick()
    _drain_thread(ctx)
    assert len(calls) == 1


def test_one_entry_per_tick(ctx, repo, monkeypatch):
    _queue(repo, "run-c1")
    _queue(repo, "run-c2")
    calls = _recorder(ctx, monkeypatch, {"result": {"committed": True}})
    ctx._idle_tick()
    _drain_thread(ctx)
    assert len(calls) == 1
    pending = [p.name for p in land_resume.queue_dir(None).glob("*" + land_resume.PENDING_SUFFIX)]
    assert len(pending) == 1
    ctx._idle_tick()
    _drain_thread(ctx)
    assert len(calls) == 2


def test_tick_does_not_start_a_second_attempt_while_one_is_in_flight(ctx, repo, monkeypatch):
    _queue(repo, "run-d1")
    _queue(repo, "run-d2")
    release = threading.Event()
    calls: list = []

    def slow(msg, **kwargs):
        calls.append(msg)
        release.wait(10)
        return {"result": {"committed": True}}

    monkeypatch.setattr(ctx, "_pool_dispatch", slow)
    ctx._idle_tick()
    ctx._idle_tick()
    try:
        assert len(calls) <= 1
        assert len(list(land_resume.queue_dir(None).glob("*" + land_resume.PENDING_SUFFIX))) == 1
    finally:
        release.set()
        _drain_thread(ctx)


def test_reply_already_requeued_by_land_run_returns_entry_to_pending(ctx, repo, monkeypatch):
    params = _queue(repo, "run-e")

    def reply(msg):
        land_resume.enqueue(None, "run-e", params, "land_run.integrity")
        return {"result": {"committed": False, "queued_for_resume": True}}

    _recorder(ctx, monkeypatch, reply)
    ctx._idle_tick()
    _drain_thread(ctx)
    pending = _entry("run-e", land_resume.PENDING_SUFFIX)
    assert pending["attempts"] == 2
    assert pending["last_leg"] == "land_run.integrity"
    assert not (land_resume.queue_dir(None) / ("run-e" + land_resume.DONE_SUFFIX)).exists()


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"code": -32004, "message": "indeterminate"}},
        {"result": {"committed": False, "refused": "queued"}},
        None,
    ],
)
def test_transient_failure_requeues_with_attempt_bumped(ctx, repo, monkeypatch, reply):
    _queue(repo, "run-f")
    _recorder(ctx, monkeypatch, reply)
    ctx._idle_tick()
    _drain_thread(ctx)
    pending = _entry("run-f", land_resume.PENDING_SUFFIX)
    assert pending["attempts"] == 2
    assert not (land_resume.queue_dir(None) / ("run-f" + land_resume.DONE_SUFFIX)).exists()


def test_final_refusal_closes_entry_without_requeue(ctx, repo, monkeypatch):
    _queue(repo, "run-g")
    _recorder(ctx, monkeypatch, {"result": {"committed": False, "refused": "unreviewed"}})
    ctx._idle_tick()
    _drain_thread(ctx)
    assert _entry("run-g", land_resume.DONE_SUFFIX)["outcome"] == "final"
    assert not (land_resume.queue_dir(None) / ("run-g" + land_resume.PENDING_SUFFIX)).exists()


def test_attempt_exception_requeues_and_never_escapes_the_tick(ctx, repo, monkeypatch):
    _queue(repo, "run-h")

    def boom(msg, **kwargs):
        raise RuntimeError("pool exploded")

    monkeypatch.setattr(ctx, "_pool_dispatch", boom)
    ctx._idle_tick()
    _drain_thread(ctx)
    assert _entry("run-h", land_resume.PENDING_SUFFIX)["attempts"] == 2


def test_retries_are_bounded_by_the_queue_expiry(ctx, repo, monkeypatch):
    _queue(repo, "run-i")
    calls = _recorder(ctx, monkeypatch, None)
    for _ in range(land_resume.MAX_ATTEMPTS + 3):
        ctx._idle_tick()
        _drain_thread(ctx)
    assert len(calls) == land_resume.MAX_ATTEMPTS
    assert _entry("run-i", land_resume.DONE_SUFFIX)["outcome"] == land_resume.OUTCOME_EXPIRED


def test_repo_resolved_from_halt_record_when_entry_names_none(ctx, repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import land_leg_record
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    _queue(repo, "run-j", repo_root=False)
    target = record_dir(repo, "run-j") / land_leg_record.FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    calls = _recorder(ctx, monkeypatch, {"result": {"committed": True}})
    ctx._idle_tick()
    _drain_thread(ctx)
    assert calls[0][0]["_origin_worktree"] == str(repo)


def test_unresolvable_repo_requeues_without_dispatch(ctx, repo, monkeypatch):
    _queue(repo, "run-k", repo_root=False)
    calls = _recorder(ctx, monkeypatch, {"result": {"committed": True}})
    ctx._idle_tick()
    _drain_thread(ctx)
    assert calls == []
    assert _entry("run-k", land_resume.PENDING_SUFFIX)["attempts"] == 2


def test_empty_queue_dispatches_nothing_and_spawns_nothing(ctx, monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("an empty land-resume queue must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    calls = _recorder(ctx, monkeypatch, {"result": {"committed": True}})
    assert ctx._land_resume_tick() is False
    ctx._idle_tick()
    assert calls == []
    assert ctx.__dict__.get("_land_resume_thread") is None


def test_queue_failure_leaves_tick_intact(ctx, monkeypatch):
    def boom(root, now):
        raise RuntimeError("queue broke")

    monkeypatch.setattr(land_resume, "next_due", boom)
    ctx._idle_tick()
