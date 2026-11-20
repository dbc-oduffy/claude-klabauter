"""Mutating handler threads that outlive an abandoned dispatch recycle the warm server at the
threshold, in memory, even when the stall class is io-wait; compute-only ops, held and backed-off
engines, and sub-threshold counts never exit."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import coordinator_core.ipc as ipc
from coordinator_core import dispatch_abandon
from coordinator_core.warm import breadcrumb, hang_restart

pytestmark = pytest.mark.complete

_THRESHOLD = hang_restart.ABANDONED_MUTATING_RECYCLE_THRESHOLD


@pytest.fixture
def rig(tmp_path, monkeypatch):
    exits: list = []
    release = threading.Event()
    monkeypatch.setattr(hang_restart, "exit_server", lambda pid: exits.append(pid))
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr("coordinator_core.session.core.in_warm_served_request", lambda: True)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.15)
    monkeypatch.setattr(ipc, "_cold_mutating_op", lambda method: False)

    def _blocker(params, repo_root=None):
        release.wait(20)
        return {}

    for i in range(_THRESHOLD + 2):
        monkeypatch.setitem(ipc._REGISTRY, f"test.abandon_mut_{i}", _blocker)
    monkeypatch.setitem(ipc._REGISTRY, "test.abandon_ro", _blocker)
    monkeypatch.setattr(
        "coordinator_core.warm.client._op_may_mutate", lambda method: method != "test.abandon_ro"
    )
    before = dispatch_abandon.abandoned_live_count()
    yield tmp_path, exits, release
    release.set()
    deadline = time.monotonic() + 5
    while dispatch_abandon.abandoned_live_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)


async def _dispatch(method: str) -> dict:
    return await ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})


async def _dispatch_all(methods, release) -> list:
    replies = [await _dispatch(m) for m in methods]
    release.set()
    return replies


def test_threshold_abandoned_mutating_threads_recycle_once_without_replay(rig):
    svc, exits, release = rig
    methods = [f"test.abandon_mut_{i}" for i in range(_THRESHOLD)]
    replies = asyncio.run(_dispatch_all(methods, release))
    assert all("error" in r for r in replies)
    assert len(exits) == 1
    log = (svc / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert " action=restart " in log and f"source={hang_restart.ABANDONED_SOURCE}" in log
    assert f"count={_THRESHOLD}" in log


def test_below_threshold_never_exits(rig):
    _svc, exits, release = rig
    asyncio.run(_dispatch_all([f"test.abandon_mut_{i}" for i in range(_THRESHOLD - 1)], release))
    assert exits == []


def test_compute_only_abandonment_is_not_counted(rig):
    _svc, exits, release = rig
    before = dispatch_abandon.abandoned_live_count()
    asyncio.run(_dispatch_all(["test.abandon_ro"] * (_THRESHOLD + 1), release))
    assert exits == []
    assert dispatch_abandon.abandoned_live_count() == before


def test_live_count_drops_when_the_handler_thread_finishes(rig):
    _svc, _exits, release = rig
    before = dispatch_abandon.abandoned_live_count()

    async def _go():
        await _dispatch("test.abandon_mut_0")
        during = dispatch_abandon.abandoned_live_count()
        release.set()
        return during

    during = asyncio.run(_go())
    assert during == before + 1
    deadline = time.monotonic() + 5
    while dispatch_abandon.abandoned_live_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert dispatch_abandon.abandoned_live_count() == before


def test_backoff_suppresses_the_recycle(rig):
    svc, exits, release = rig
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (svc / hang_restart.HANG_RESTART_LOG_FILENAME).write_text(
        f"{stamp} pid=1 action=restart count=8 window_s=60 backoff_s=0 dumps=\n", encoding="utf-8"
    )
    asyncio.run(_dispatch_all([f"test.abandon_mut_{i}" for i in range(_THRESHOLD)], release))
    assert exits == []
    log = (svc / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert "action=suppressed reason=backoff" in log


def test_io_wait_dumps_do_not_carry_the_io_stall_no_exit_carve_out(tmp_path):
    stall = tmp_path / "stall-dumps"
    stall.mkdir()
    for i in range(hang_restart.HANG_DUMP_THRESHOLD + 1):
        (stall / f"2026111818{i:02d}00Z-1-k{i}.txt").write_text(
            f"{hang_restart.IO_WAIT_HEADER}\n", encoding="utf-8"
        )
    exits: list = []
    verdict = hang_restart.maybe_restart_on_abandoned(
        _THRESHOLD, svc_dir=tmp_path, server_pid=4242, exit_fn=exits.append
    )
    assert verdict == "restart" and exits == [4242]
    assert hang_restart.maybe_restart_on_abandoned(
        _THRESHOLD - 1, svc_dir=tmp_path, server_pid=4242, exit_fn=exits.append
    ) is None
    assert exits == [4242]


def test_publish_hold_suppresses_the_recycle(tmp_path, monkeypatch):
    monkeypatch.setattr(breadcrumb, "publish_marker_held", lambda svc, now=None: True)
    exits: list = []
    verdict = hang_restart.maybe_restart_on_abandoned(
        _THRESHOLD, svc_dir=tmp_path, server_pid=4242, exit_fn=exits.append
    )
    assert verdict == "suppressed" and exits == []
    log = (tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert "action=suppressed reason=publish-hold" in log


def test_unfinished_decision_leg_restarts_anyway(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(hang_restart, "_tail_lines", lambda *_a, **_k: release.wait(5) and [])
    exits: list = []
    try:
        verdict = hang_restart.maybe_restart_on_abandoned(
            _THRESHOLD, svc_dir=tmp_path, server_pid=4242, exit_fn=exits.append, deadline=0.05
        )
    finally:
        release.set()
    assert verdict == "restart" and exits == [4242]
