"""A warm op that outlives its deadline reconciles to a definite state.

The poll reads `executing` while the op runs (never `unknowable/abandoned` from a
premature stamp, never `finished` from a wrapper reply that only carries the nested
op's -32004), then `finished` with the op's own outcome. A door-form
`review.prep` frame is judged by the op it runs when `_COLD_REPLAY_SAFE` is asked.
"""

from __future__ import annotations

import json
import threading

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.warm import client, dispatch_ack, server

_OP = "review.prep"


@pytest.fixture
def store(monkeypatch):
    fresh = dispatch_ack.AckStore()
    monkeypatch.setattr(server, "_ack_store", fresh)
    return fresh


def _admit(store) -> str:
    key = dispatch_ack.mint_key()
    assert store.admit(key, "invoke.from_argv")
    return key


def _indeterminate_envelope() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": server.WARM_DISPATCH_INDETERMINATE, "message": "timed out and is hung"},
    }


def _from_argv_reply(stdout: str, exit_code: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {"stdout": stdout, "stderr": "", "exit_code": exit_code}}


class _Done:
    def __init__(self, result):
        self._result = result

    def cancelled(self):
        return False

    def exception(self):
        return None

    def result(self):
        return self._result


def test_unknown_key_reads_not_received(store):
    assert server._store_poll({"key": dispatch_ack.mint_key()})["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_wrapper_reply_carrying_the_nested_timeout_leaves_the_record_executing(store):
    key = _admit(store)
    reply = _from_argv_reply(json.dumps(_indeterminate_envelope()) + "\n")
    server._stamp_pool_future(key, _Done(reply))
    assert server._store_poll({"key": key})["state"] == dispatch_ack.STATE_EXECUTING

    landed = {"jsonrpc": "2.0", "id": 1, "result": {"slices": 3}}
    server._stamp_dispatch_outcome(key, landed)
    status = server._store_poll({"key": key})
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT
    assert status["result"] == {"slices": 3}


def test_wrapper_reply_with_an_ordinary_failure_still_finishes(store):
    key = _admit(store)
    server._stamp_dispatch_outcome(key, _from_argv_reply('{"error": {"code": -32602}}\n'))
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED


def test_a_direct_indeterminate_envelope_still_stamps_abandoned(store):
    key = _admit(store)
    server._stamp_dispatch_outcome(key, _indeterminate_envelope())
    assert store.status(key) == {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.OUTCOME_ABANDONED}


def test_timed_out_inline_op_reads_executing_then_finished(store, monkeypatch):
    key = _admit(store)
    seen: dict = {}

    async def _times_out(msg, *, caller=None, corr_id=None):
        late = ipc.LATE_COMPLETION.get()
        seen["late"] = late
        late.mark_timed_out(msg.get("id"))
        return _indeterminate_envelope()

    monkeypatch.setattr(server, "dispatch_message", _times_out, raising=False)
    monkeypatch.setattr(ipc, "dispatch_message", _times_out)

    out = server._run_dispatch({"id": 1, "method": "invoke.from_argv"}, caller=None, dispatch_key=key)

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert server._store_poll({"key": key})["state"] == dispatch_ack.STATE_EXECUTING

    seen["late"].complete(value={"slices": 2})
    status = server._store_poll({"key": key})
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"] == {"slices": 2}


def test_an_executing_record_that_never_completes_still_reads_terminal(monkeypatch):
    bounded = dispatch_ack.AckStore(executing_bound=lambda method: 1.0)
    key = _admit(bounded)
    bounded._records[key]["admitted_mono"] -= 10_000

    assert bounded.status(key) == {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.OUTCOME_ABANDONED}


def test_handler_still_running_is_false_without_a_timeout():
    late = ipc.LateCompletion()
    assert not server._handler_still_running(late)
    late.mark_timed_out(1)
    assert server._handler_still_running(late)
    late.complete(value={})
    assert not server._handler_still_running(late)


class _StuckPipe:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        threading.Event().wait(30)
        return b""

    def close(self) -> None:
        pass


@pytest.fixture
def stuck_warm(monkeypatch):
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)
    pipe = _StuckPipe()
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipe)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    return pipe


def _door_msg(*argv, entrypoint=None) -> dict:
    params: dict = {"argv": list(argv), "cwd": "."}
    if entrypoint:
        params["entrypoint"] = entrypoint
    return {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv", "params": params}


@pytest.mark.parametrize(
    "argv",
    [
        ("review.prep", "{}", "--bare"),
        ("--repo", "/some/repo", "review.prep", "{}"),
        ("--bare", "review.prep"),
    ],
)
def test_door_form_review_prep_names_the_op_it_runs(argv):
    assert client._replay_op_name(_door_msg(*argv)) == _OP


@pytest.mark.parametrize(
    "msg",
    [
        _door_msg("review.prep", entrypoint="some-cli"),
        _door_msg(),
        _door_msg("--repo"),
        {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv", "params": {"argv": [3]}},
    ],
)
def test_other_door_frames_stay_the_wrapper_method(msg):
    assert client._replay_op_name(msg) == "invoke.from_argv"


def test_door_form_review_prep_expiry_goes_cold_once(stuck_warm):
    response = client.try_warm_dispatch(_door_msg("review.prep", "{}", "--bare"))
    assert response is None
    assert len(stuck_warm.written) == 1
    assert client._last_cold_bucket == client.COLD_BUCKET_MUTATION_DEADLINE_COLD_REPLAY


def test_door_form_other_op_expiry_still_returns_indeterminate(stuck_warm):
    response = client.try_warm_dispatch(_door_msg("ceremony.scoped_git_commit", "{}"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE


def test_replay_safe_entrypoint_expiry_goes_cold_once(stuck_warm, capsys):
    response = client.try_warm_dispatch(_door_msg("brief", "--repo", "x", entrypoint="consolidate-assemble"))
    assert response is None
    assert len(stuck_warm.written) == 1
    assert client._last_cold_bucket == client.COLD_BUCKET_MUTATION_DEADLINE_COLD_REPLAY
    assert "consolidate-assemble brief" in capsys.readouterr().err


@pytest.mark.parametrize(
    "msg",
    [
        _door_msg("apply", entrypoint="consolidate-assemble"),
        _door_msg("brief", entrypoint="other-cli"),
        _door_msg("--repo", "x", "brief", entrypoint="consolidate-assemble"),
        _door_msg(entrypoint="consolidate-assemble"),
        _door_msg("brief"),
    ],
)
def test_other_entrypoint_frames_expiry_still_returns_indeterminate(stuck_warm, msg):
    response = client.try_warm_dispatch(msg)
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE


def test_value_flag_set_matches_the_invoke_parser():
    from coordinator_core.invoke.__main__ import _build_arg_parser

    taking_a_value = {
        opt
        for action in _build_arg_parser()._actions
        if action.option_strings and action.nargs != 0
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }
    assert client._FROM_ARGV_VALUE_FLAGS == taking_a_value


def test_from_task_output_value_is_not_read_as_the_op():
    msg = {"method": "invoke.from_argv", "params": {"argv": ["--from-task-output", "review.prep", "dispatch.land_run"]}}
    assert client._replay_op_name(msg) == "dispatch.land_run"
