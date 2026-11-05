"""The engine-ask submit op rides the op door's remote failover when the local door is unreachable."""

from __future__ import annotations

import json
import subprocess

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.group_em import box_transport
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.remote_engine import fallback, policy

OP = "hooks.submit_engine_ask"
SSH = {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}
BOXES = [
    {"box_id": "local", "root": "/r", "capability": {}, "transport": SSH},
    {"box_id": "far", "root": "/r", "capability": {}, "transport": SSH},
]
FAR_REPLY = {"run_id": "ask-far", "branch": "work/far/2026-11-04"}


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": FAR_REPLY}), "")


@pytest.fixture
def runner(monkeypatch, tmp_path):
    r = Runner()
    monkeypatch.setattr(box_transport, "_run", r)
    monkeypatch.setattr(fallback.box_registry, "read_boxes", lambda *a, **k: BOXES)
    monkeypatch.setattr(fallback.box_registry, "local_box_id", lambda *a, **k: "local")
    monkeypatch.setattr(fallback, "telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    monkeypatch.delenv(fallback.HOOK_ENV, raising=False)
    return r


@pytest.fixture(autouse=True)
def _local_handler():
    saved = ipc._REGISTRY.get(OP)
    ipc._REGISTRY[OP] = lambda params, ctx=None, repo_root=None: {"local": True}
    yield
    if saved is None:
        ipc._REGISTRY.pop(OP, None)
    else:
        ipc._REGISTRY[OP] = saved


def _submit(monkeypatch, tmp_path, *, reason):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", False)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.settings.is_warm_enabled", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", lambda msg, **kw: None)
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: reason)
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait", lambda **kw: None
    )
    return _dispatch_argv([OP, "{}"], str(tmp_path), allow_warm=True)


def test_submit_op_is_remote_safe_not_local_only():
    assert policy.remote_safe(OP)
    assert not policy.is_local_only(OP)


def test_unreachable_local_door_launches_background_retry_and_never_runs_locally(
    monkeypatch, tmp_path, runner
):
    from coordinator_core.remote_engine import background

    launched = []
    monkeypatch.setattr(
        background, "launch", lambda op, params, **kw: launched.append((op, params)) or "key123"
    )
    stdout, stderr, code = _submit(
        monkeypatch, tmp_path, reason="warm engine: resolved engine root does not exist: /x"
    )
    assert launched == [(OP, {})]
    assert runner.calls == []
    out = json.loads(stdout)
    assert "result" not in out
    assert out["remote_handle"] == "key123"
    assert out["error"]["data"] == {"remote_handle": "key123", "pending": True}
    assert code != 0
    assert "handle key123" in stderr


def test_no_registered_peer_keeps_the_local_path(monkeypatch, tmp_path, runner):
    monkeypatch.setattr(fallback.box_registry, "read_boxes", lambda *a, **k: BOXES[:1])
    stdout, _stderr, code = _submit(monkeypatch, tmp_path, reason="ENGINE_SKEW")
    assert runner.calls == []
    assert code == 0 and json.loads(stdout)["result"] == {"local": True}
