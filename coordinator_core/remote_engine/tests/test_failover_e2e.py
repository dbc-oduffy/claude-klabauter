"""The op door's remote failover seam: reach miss or failed health probe -> one remote retry."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.group_em import box_transport
from coordinator_core.invoke.__main__ import _dispatch_argv, _remote_failover
from coordinator_core.remote_engine import background, fallback

OP = "session.peer_roster"
SSH = {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}
BOXES = [
    {"box_id": "local", "root": "/r", "capability": {}, "transport": SSH},
    {"box_id": "far", "root": "/r", "capability": {}, "transport": SSH},
]


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, '{"result": {"rows": ["remote"]}}', "")


@pytest.fixture
def runner(monkeypatch, tmp_path):
    r = Runner()
    monkeypatch.setattr(box_transport, "_run", r)
    monkeypatch.setattr(fallback.box_registry, "read_boxes", lambda *a, **k: BOXES)
    monkeypatch.setattr(fallback.box_registry, "local_box_id", lambda *a, **k: "local")
    monkeypatch.setattr(fallback, "telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    return r


class Spawner:
    """Records each launch the door made (the handle key); `fail` makes launch return None."""

    def __init__(self):
        self.launched_keys = []
        self.fail = False


@pytest.fixture
def spawner(monkeypatch, tmp_path):
    sp = Spawner()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    real = background.launch

    def launch(op, params, popen=None):
        key = None if sp.fail else real(op, params)
        if key is not None:
            sp.launched_keys.append(key)
        return key

    monkeypatch.setattr(background, "launch", launch)
    return sp


PHASE2 = "dispatch.emit"


@pytest.fixture
def phase2_spy():
    calls = []
    saved = ipc._REGISTRY.get(PHASE2)
    ipc._REGISTRY[PHASE2] = lambda params, ctx=None, repo_root=None: calls.append(params) or {"minted": 1}
    yield calls
    if saved is None:
        ipc._REGISTRY.pop(PHASE2, None)
    else:
        ipc._REGISTRY[PHASE2] = saved


@pytest.fixture(autouse=True)
def _local_handler():
    saved = ipc._REGISTRY.get(OP)
    ipc._REGISTRY[OP] = lambda params, ctx=None, repo_root=None: {"pong": True}
    yield
    if saved is None:
        ipc._REGISTRY.pop(OP, None)
    else:
        ipc._REGISTRY[OP] = saved


def _run(monkeypatch, tmp_path, *, op=OP, boot_wait="5", reason=None, allow_unstamped=False, params="{}"):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", boot_wait)
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", allow_unstamped)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.settings.is_warm_enabled", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", lambda msg, **kw: None)
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: reason)
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait", lambda **kw: None
    )
    return _dispatch_argv([op, params], str(tmp_path), allow_warm=True)


def test_failed_health_probe_launches_background_retry_and_local_stands(
    monkeypatch, tmp_path, runner, spawner
):
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, reason="warm engine: resolved engine root does not exist: /x"
    )
    assert code == 0
    assert runner.calls == []
    assert len(spawner.launched_keys) == 1
    out = json.loads(stdout)
    assert out["result"]["pong"] is True
    key = out["remote_handle"]
    assert out["result"]["remote_handle"] == key
    assert spawner.launched_keys == [key]
    assert background.read_handle(key)["status"] == "pending"
    assert not (tmp_path / "state").exists()


def test_phase2_op_returns_pending_error_and_never_runs_locally(
    monkeypatch, tmp_path, runner, spawner, phase2_spy
):
    stdout, _stderr, code = _run(
        monkeypatch, tmp_path, op=PHASE2, reason="warm engine: resolved engine root does not exist: /x"
    )
    out = json.loads(stdout)
    assert code == 1 and "error" in out
    assert out["error"]["data"]["pending"] is True
    assert background.read_handle(out["error"]["data"]["remote_handle"])["op"] == PHASE2
    assert phase2_spy == []
    assert len(spawner.launched_keys) == 1 and runner.calls == []


def test_phase2_launch_failure_reports_not_run_and_never_runs_locally(
    monkeypatch, tmp_path, runner, spawner, phase2_spy
):
    spawner.fail = True
    stdout, _stderr, code = _run(
        monkeypatch, tmp_path, op=PHASE2, reason="warm engine: resolved engine root does not exist: /x"
    )
    out = json.loads(stdout)
    assert code == 1 and "remote launch failed" in out["error"]["message"]
    assert phase2_spy == []


def test_phase2_with_local_path_param_stays_local(monkeypatch, tmp_path, runner, spawner, phase2_spy):
    _run(
        monkeypatch,
        tmp_path,
        op=PHASE2,
        reason="warm engine: resolved engine root does not exist: /x",
        params='{"cwd": "/abs/tree"}',
    )
    assert spawner.launched_keys == [] and len(phase2_spy) == 1


def test_phase2_in_hook_context_stays_local(monkeypatch, tmp_path, runner, spawner, phase2_spy):
    monkeypatch.setenv("COORDINATOR_HOOK_CONTEXT", "1")
    _run(monkeypatch, tmp_path, op=PHASE2, reason="warm engine: resolved engine root does not exist: /x")
    assert spawner.launched_keys == [] and len(phase2_spy) == 1


def test_hot_path_budget_failover_returns_pending_without_ssh(monkeypatch, tmp_path, runner, spawner):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", False)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    calls = []
    monkeypatch.setattr(box_transport, "invoke_remote", lambda *a, **k: calls.append(a))
    t0 = time.process_time()
    envelope = _remote_failover({"jsonrpc": "2.0", "id": 1, "method": OP, "params": {}})
    assert time.process_time() - t0 < 0.05
    assert envelope["remote_handle"] and "error" not in envelope
    assert calls == [] and runner.calls == []


def test_child_writes_indeterminate_handle_with_reconcile_instruction(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    key = background.launch(PHASE2, {}, popen=lambda argv, **kw: None)
    served = {"indeterminate": True, "served_by": "far", "reason": "timeout", "request_key": "k1"}
    assert background.run_child(key, try_remote=lambda op, params: served) == 0
    handle = background.read_handle(key)
    assert handle["status"] == "indeterminate"
    assert "warm.request_status" in handle["instruction"] and "k1" in handle["instruction"]


def test_child_runs_try_remote_directly_and_records_reply(monkeypatch, tmp_path, runner):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    key = background.launch(OP, {}, popen=lambda argv, **kw: None)
    assert background.run_child(key) == 0
    handle = background.read_handle(key)
    assert handle["status"] == "done" and handle["served_by"] == "far"
    assert handle["reply"]["rows"] == ["remote"]
    assert len(runner.calls) == 1


SECRET = {"prompt": "SECRET-PROMPT-BODY"}


def _files():
    return sorted(p.name for p in background.requests_dir().iterdir())


def test_handle_files_never_hold_raw_params_and_child_consumes_the_side_file(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    key = background.launch(PHASE2, SECRET, popen=lambda argv, **kw: None)
    assert "SECRET-PROMPT-BODY" not in background._handle_path(key).read_text()
    seen = []
    served = {"served_by": "far", "reply": {"ok": 1}}
    assert background.run_child(key, try_remote=lambda op, p: seen.append(p) or served) == 0
    assert seen == [SECRET]
    assert _files() == [f"{key}.json"]
    assert "SECRET-PROMPT-BODY" not in background._handle_path(key).read_text()


def test_sweep_removes_only_files_older_than_ttl(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    key = background.launch(OP, SECRET, popen=lambda argv, **kw: None)
    assert background.sweep(60, now=time.time()) == 0
    assert len(_files()) == 2
    assert background.sweep(60, now=time.time() + 3600) == 2
    assert background.read_handle(key) is None


def test_launch_sweeps_expired_handles(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    old = background.launch(OP, {}, popen=lambda argv, **kw: None)
    aged = time.time() - 2 * background.HANDLE_TTL_S
    os.utime(background._handle_path(old), (aged, aged))
    fresh = background.launch(OP, {}, popen=lambda argv, **kw: None)
    assert background.read_handle(old) is None
    assert background.read_handle(fresh) is not None


def test_handle_op_reads_outcome_and_answers_not_found(monkeypatch, tmp_path):
    from coordinator_core.ops.remote_engine_handle import _remote_engine_handle

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    key = background.launch(PHASE2, {}, popen=lambda argv, **kw: None)
    assert _remote_engine_handle({"key": key}) == {
        "found": True,
        "handle": {"status": "pending", "op": PHASE2},
    }
    assert _remote_engine_handle({"key": "missing"}) == {"found": False, "handle": None}
    with pytest.raises(ValueError):
        _remote_engine_handle({})


def test_launch_spawns_no_process_and_costs_little(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))

    def boom(*a, **k):
        raise AssertionError("launch must not spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    t0 = time.process_time()
    key = background.launch(OP, {})
    assert key and background.read_handle(key)["status"] == "pending"
    assert time.process_time() - t0 < 0.05


def test_drain_pending_runs_queued_entry_through_node_transport(monkeypatch, tmp_path, runner):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    key = background.launch(OP, {})
    assert background.drain_pending() == 1
    assert background.read_handle(key)["status"] == "done"
    assert len(runner.calls) == 1
    assert background.drain_pending() == 0 and len(runner.calls) == 1


def test_drain_pending_is_bounded_per_call(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    keys = [background.launch(OP, {}) for _ in range(background.DRAIN_BATCH + 2)]
    assert background.drain_pending(try_remote=lambda op, p: None) == background.DRAIN_BATCH
    statuses = [background.read_handle(k)["status"] for k in keys]
    assert statuses.count("pending") == 2


def test_second_claim_of_the_same_entry_runs_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    key = background.launch(OP, {})
    background._params_path(key).unlink()
    assert background.run_child(key, try_remote=lambda op, p: pytest.fail("ran")) == 1


def test_fallback_refuses_a_non_ssh_box(monkeypatch, tmp_path, runner):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    monkeypatch.setattr(fallback, "pick_box", lambda boxes, lid: {"box_id": "far", "transport": {"kind": "weird"}})
    assert fallback.try_remote(OP, {}) is None
    assert runner.calls == []


def test_read_handle_rejects_traversal_and_missing():
    assert background.read_handle("../x") is None
    assert background.read_handle("nope") is None


@pytest.mark.deliberate_wall_clock(reason="fail-open bound: a hook context must not wait on a remote boot")
def test_hook_context_makes_no_remote_call_and_fails_open_fast(monkeypatch, tmp_path, runner):
    t0 = time.monotonic()
    stdout, _stderr, code = _run(
        monkeypatch, tmp_path, boot_wait="0", reason="warm engine: resolved engine root does not exist: /x"
    )
    assert time.monotonic() - t0 < 1.0
    assert runner.calls == []
    assert code == 0 and json.loads(stdout)["result"] == {"pong": True}


def test_viable_cold_run_makes_zero_runner_calls(monkeypatch, tmp_path, runner):
    stdout, _stderr, code = _run(monkeypatch, tmp_path, boot_wait="0", reason=None)
    assert code == 0 and json.loads(stdout)["result"] == {"pong": True}
    assert runner.calls == []


def test_cold_by_design_clone_stays_local(monkeypatch, tmp_path, runner):
    reason = "warm engine: this clone carries no engine build stamp, so it is not a warm-server host"
    stdout, _stderr, code = _run(monkeypatch, tmp_path, reason=reason)
    assert code == 0 and json.loads(stdout)["result"] == {"pong": True}
    assert runner.calls == []


def test_allow_unstamped_dispatch_never_goes_remote(monkeypatch, tmp_path, runner):
    stdout, _stderr, code = _run(
        monkeypatch, tmp_path, reason="ENGINE_SKEW", allow_unstamped=True
    )
    assert code == 0 and json.loads(stdout)["result"] == {"pong": True}
    assert runner.calls == []


def test_trim_file_cache_never_routes_remotely(monkeypatch, tmp_path, runner):
    _run(monkeypatch, tmp_path, op="trim-file-cache", reason="ENGINE_SKEW")
    assert runner.calls == []


def test_cold_dispatch_overrun_launches_background_retry_once(monkeypatch, tmp_path, runner, spawner):
    ipc._REGISTRY[OP] = lambda params, ctx=None, repo_root=None: time.sleep(1.5) or {"pong": True}
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.3")
    stdout, _stderr, _code = _run(monkeypatch, tmp_path, reason=None, boot_wait="0.2")
    assert runner.calls == []
    assert len(spawner.launched_keys) == 1
    assert background.read_handle(json.loads(stdout)["remote_handle"])["status"] == "pending"
