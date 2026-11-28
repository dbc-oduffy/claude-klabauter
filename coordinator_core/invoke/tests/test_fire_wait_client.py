"""workflow.fire_wait client leg: the cold CLI loops, the warm server refuses so the door runs it cold."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.invoke import __main__ as invoke_main
from coordinator_core.invoke import dispatch as invoke_dispatch

REPO = str(Path(__file__).resolve().parents[3])
FIRE_ID = "fire-test-1"


def _reply(state, *, timed_out=False, timeout_s=7200, found=True, deadline_at=None):
    exited = state == "exited"
    return {
        "fire_id": FIRE_ID,
        "found": found,
        "state": state,
        "outcome": "landed" if exited else None,
        "run_id": "run-1" if exited else None,
        "commit_sha": "abc123" if exited else None,
        "commit_sha_reason": None if exited else "run not exited",
        "timed_out": timed_out,
        "exit_code": 0 if exited else None,
        "timeout_s": timeout_s,
        "deadline_at": deadline_at,
    }


def _install_fake_engine(monkeypatch, replies):
    calls = []
    queue = list(replies)

    async def fake_dispatch(msg, caller=None):
        calls.append(msg)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": queue.pop(0) if len(queue) > 1 else queue[0]}

    monkeypatch.setattr(invoke_dispatch, "dispatch_message", fake_dispatch)
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return calls, sleeps


def _run(params: dict, *, allow_warm=True):
    return invoke_main._dispatch_argv(
        [invoke_main._FIRE_WAIT_OP, json.dumps(params)], REPO, allow_warm=allow_warm
    )


def test_default_invocation_blocks_through_running_then_prints_one_final_json_line(monkeypatch):
    calls, sleeps = _install_fake_engine(
        monkeypatch, [_reply("running"), _reply("running"), _reply("exited")]
    )

    stdout, _stderr, code = _run({"fire_id": FIRE_ID})

    assert code == 0
    assert len(calls) == 3
    assert sleeps == [5.0, 5.0]
    lines = stdout.strip().splitlines()
    assert len(lines) == 1
    final = json.loads(lines[0])
    assert (final["state"], final["outcome"], final["run_id"], final["commit_sha"]) == (
        "exited", "landed", "run-1", "abc123",
    )


def test_failed_fire_returns_nonzero_with_terminal_receipt(monkeypatch):
    failed = dict(_reply("exited"), outcome="failed", exit_code=1, commit_sha=None,
                  reason="criterion not_met: The specified rebase was not observed.")
    calls, sleeps = _install_fake_engine(monkeypatch, [_reply("running"), failed])

    stdout, _stderr, code = _run({"fire_id": FIRE_ID})

    assert code == 1
    assert json.loads(stdout) == failed
    assert len(calls) == 2 and len(sleeps) == 1


@pytest.mark.parametrize(("outcome", "reason", "expected"), [
    ("not_landed", "run stopped at staged", 1),
    ("denied", "driver refused", 1),
    ("truncated", "driver stopped early", 1),
    ("clean", None, 0),
])
def test_terminal_disposition_without_driver_exit(monkeypatch, outcome, reason, expected):
    terminal = dict(_reply("exited"), outcome=outcome, reason=reason, commit_sha=None, exit_code=None)
    _install_fake_engine(monkeypatch, [terminal])

    stdout, _, code = _run({"fire_id": FIRE_ID})

    assert json.loads(stdout) == terminal
    assert code == expected


def test_terminal_failure_sets_process_exit_and_keeps_final_receipt(monkeypatch):
    failed = {**_reply("exited"), "exit_code": 7, "outcome": "failed", "commit_sha": None}
    calls, sleeps = _install_fake_engine(monkeypatch, [failed])
    stdout, stderr, code = _run({"fire_id": FIRE_ID})
    assert code == 1
    assert json.loads(stdout) == failed
    assert "process exit 1" in stderr
    assert len(calls) == 1 and not sleeps


def test_poll_interval_is_clamped_to_one_to_sixty(monkeypatch):
    _calls, sleeps = _install_fake_engine(monkeypatch, [_reply("running"), _reply("running"), _reply("exited")])
    _run({"fire_id": FIRE_ID, "poll_interval_s": 0.01})
    assert sleeps == [1.0, 1.0]

    _calls, sleeps = _install_fake_engine(monkeypatch, [_reply("running"), _reply("exited")])
    _run({"fire_id": FIRE_ID, "poll_interval_s": 9999})
    assert sleeps == [60.0]


def test_engine_reported_timeout_returns_timed_out_true_and_exit_one(monkeypatch):
    calls, _sleeps = _install_fake_engine(
        monkeypatch, [_reply("running"), _reply("running", timed_out=True)]
    )

    stdout, _stderr, code = _run({"fire_id": FIRE_ID})

    assert code == 1
    assert len(calls) == 2
    final = json.loads(stdout)
    assert final["timed_out"] is True
    assert final["state"] == "running"


def test_client_deadline_bounds_a_record_the_engine_never_times_out():
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    ticks = []

    def tick(msg):
        ticks.append(1)
        return {"result": _reply("running", timeout_s=12)}

    response, code = invoke_main._wait_for_fire(
        {"id": 1, "params": {"fire_id": FIRE_ID, "poll_interval_s": 5}},
        dispatch_tick=tick,
        sleep=sleep,
        monotonic=lambda: clock[0],
    )

    assert code == 1
    assert response["result"]["timed_out"] is True
    assert sleeps == [5.0, 5.0, 2.0]
    assert len(ticks) == 4


def test_unknown_fire_returns_at_once_with_exit_one(monkeypatch):
    calls, sleeps = _install_fake_engine(monkeypatch, [_reply(None, found=False)])

    stdout, _stderr, code = _run({"fire_id": FIRE_ID})

    assert code == 1
    assert len(calls) == 1
    assert sleeps == []
    assert json.loads(stdout)["found"] is False


def test_warm_server_leg_refuses_so_the_door_runs_it_cold(monkeypatch):
    calls, sleeps = _install_fake_engine(monkeypatch, [_reply("exited")])

    with pytest.raises(invoke_main.FireWaitNotWarmServedError) as excinfo:
        _run({"fire_id": FIRE_ID}, allow_warm=False)

    assert ipc._handler_exception_error(excinfo.value)["code"] == ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR
    assert calls == []
    assert sleeps == []


def test_invoke_from_argv_op_propagates_the_refusal_marker():
    from coordinator_core.ops.invoke_from_argv import _invoke_from_argv

    with pytest.raises(invoke_main.FireWaitNotWarmServedError):
        _invoke_from_argv(
            {"argv": [invoke_main._FIRE_WAIT_OP, json.dumps({"fire_id": FIRE_ID})], "cwd": REPO}
        )


def test_other_ops_on_the_warm_server_leg_are_still_served(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stdout, _stderr, code = invoke_main._dispatch_argv(["ping"], str(tmp_path), allow_warm=False)

    assert code == 0
    assert stdout.strip()
