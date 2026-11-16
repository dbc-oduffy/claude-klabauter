"""The submit hook answers inside the door's hook read deadline, so the door relays its answer."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

repo = real.repo

DOOR_DIR = Path(mod.__file__).resolve().parents[2] / "coordinator_core" / "warm" / "door"
DOOR_WINDOWS_C = DOOR_DIR / "door.c"
DOOR_POSIX_C = DOOR_DIR / "door_posix.c"

#: Door-side clock start to `evaluate`'s: connect, dispatch, payload parse; plus `_answer_overran` and the write-back.
DOOR_PRE_EVALUATE_AND_WRITEBACK_MARGIN_S = 0.3


def _define(source: Path, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{macro} missing from {source.name}"
    return int(match.group(1))


def test_the_hook_deadline_constant_is_the_doors_define_in_both_doors():
    for door in (DOOR_WINDOWS_C, DOOR_POSIX_C):
        assert _define(door, "DOOR_HOOK_SUBMIT_READ_DEADLINE_MS") == mod._DOOR_HOOK_READ_DEADLINE_S * 1000, door.name
        assert _define(door, "DOOR_HOOK_READ_DEADLINE_MS") == 500, door.name


def test_the_total_budget_constant_is_the_doors_define_and_sits_under_the_caller_budget():
    for door in (DOOR_WINDOWS_C, DOOR_POSIX_C):
        assert _define(door, "DOOR_HOOK_SUBMIT_TOTAL_MS") == mod._DOOR_HOOK_TOTAL_S * 1000, door.name
    assert mod._DOOR_HOOK_READ_DEADLINE_S < mod._DOOR_HOOK_TOTAL_S < mod._CALLER_BUDGET_S


def test_default_budget_answers_inside_the_door_deadline(monkeypatch):
    monkeypatch.delenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", raising=False)
    answer_at = mod._caller_budget_s() * mod._ANSWER_SHARE + mod._SCAN_GRACE_S
    assert answer_at + DOOR_PRE_EVALUATE_AND_WRITEBACK_MARGIN_S < mod._DOOR_HOOK_READ_DEADLINE_S


def test_budget_is_the_lesser_of_the_env_and_the_door_deadline(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "30")
    assert mod._caller_budget_s() == mod._DOOR_HOOK_READ_DEADLINE_S
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    assert mod._caller_budget_s() == 2.5
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "junk")
    assert mod._caller_budget_s() == mod._DOOR_HOOK_READ_DEADLINE_S


def test_engine_wait_is_the_pool_submit_to_bind_wait_and_zero_when_unset():
    from coordinator_core.ops.dispatch_emit import land_deadline

    assert mod._engine_wait_s() == 0.0
    land_deadline.set_queue_wait(2.5)
    try:
        assert mod._engine_wait_s() == pytest.approx(2.5)
    finally:
        land_deadline.set_queue_wait(0.0)


def test_engine_wait_prefers_the_later_of_the_enqueue_stamp_and_the_pool_wait(monkeypatch):
    import sys
    import types

    stamp = types.SimpleNamespace(value=(time.time() - 3.0, time.time(), 0))
    monkeypatch.setitem(sys.modules, "coordinator_core.warm.server", types.SimpleNamespace(_REQUEST_STAMP=stamp))
    assert mod._engine_wait_s() == pytest.approx(3.0, abs=0.2)


def _reset_queue_wait() -> None:
    from coordinator_core.ops.dispatch_emit import land_deadline

    land_deadline.set_queue_wait(0.0)


def test_the_enqueue_stamp_reaches_the_hook_through_the_pool_worker_path(monkeypatch):
    from coordinator_core.warm import server

    stamp = (time.time() - 4.0, time.time(), 0)
    forwarded = server._enqueue_monotonic(stamp)
    assert time.monotonic() - forwarded == pytest.approx(4.0, abs=0.3)

    seen: dict = {}

    def body(msg, caller):
        seen["msg"] = dict(msg)
        seen["wait"] = mod._engine_wait_s()
        return {"ok": True}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", body)
    monkeypatch.setattr(server, "_journal_landed", lambda *a, **k: None)
    msg = {"method": "noop", server._SUBMIT_STAMP_KEY: time.monotonic(), server._ENQUEUE_STAMP_KEY: forwarded}
    try:
        server._pool_dispatch_worker(msg, None)
    finally:
        server._CONNECTION_ENQUEUE_MONOTONIC = None
        _reset_queue_wait()
    assert server._ENQUEUE_STAMP_KEY not in seen["msg"]
    assert seen["wait"] == pytest.approx(4.0, abs=0.3)  # submit-to-bind alone would read ~0


def test_a_task_without_a_forwarded_stamp_reads_no_stale_enqueue_wait(monkeypatch):
    from coordinator_core.warm import server

    server._CONNECTION_ENQUEUE_MONOTONIC = time.monotonic() - 9.0  # a prior task's leftover
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", lambda msg, caller: {"ok": True})
    monkeypatch.setattr(server, "_journal_landed", lambda *a, **k: None)
    try:
        server._pool_dispatch_worker({"method": "noop"}, None)
        assert server._CONNECTION_ENQUEUE_MONOTONIC is None
    finally:
        server._CONNECTION_ENQUEUE_MONOTONIC = None
        _reset_queue_wait()


@pytest.mark.deliberate_wall_clock(reason="a late-started hook must answer at the door-relative deadline; blocked time burns no CPU")
def test_a_hook_started_late_answers_by_the_door_clock_not_its_own_start(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2")
    monkeypatch.setattr(mod, "_engine_wait_s", lambda: 1.2)  # leaves 0.5s of the 1.7s answer share
    release = threading.Event()

    def slow_fanout(params, root, abort=None, ledger=None):
        release.wait(10)
        return {"applied": False}

    asks = [{"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"]} for i in range(3)]
    try:
        started = time.monotonic()
        env = mod.evaluate(real._payload(repo, {"asks": asks}), fanout=slow_fanout)
        took = time.monotonic() - started
    finally:
        release.set()
    assert took < 1.2, took  # its own start would have waited the full 1.7s
    assert "spooled as" in env["hookSpecificOutput"]["additionalContext"]


def test_a_hook_with_no_engine_wait_keeps_the_full_answer_share(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2")
    monkeypatch.setattr(mod, "_engine_wait_s", lambda: 0.0)
    seen: list = []
    real_overran = mod._answer_overran
    monkeypatch.setattr(mod, "_answer_overran", lambda call: seen.append(call) or real_overran(call))
    mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed"}))
    assert seen == []  # a prompt submission is not cut short


@pytest.mark.deliberate_wall_clock(reason="bounds the hook deadline against a blocked fan-out; blocked time burns no CPU")
def test_multi_ask_fanout_slower_than_the_budget_denies_naming_the_hook_budget(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1")
    release = threading.Event()

    def slow_fanout(params, root, abort=None, ledger=None):
        release.wait(10)
        return {"applied": False}

    asks = [{"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"]} for i in range(3)]
    try:
        started = time.monotonic()
        env = mod.evaluate(real._payload(repo, {"asks": asks}), fanout=slow_fanout)
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "spooled as" in hso["additionalContext"]


def test_the_server_names_the_hook_reads_for_engine_wait_exist():
    from coordinator_core.warm import server

    assert hasattr(server, "_REQUEST_STAMP") and hasattr(server, "_CONNECTION_ENQUEUE_MONOTONIC")
