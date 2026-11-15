"""A warm machine-local read that never answers is the typed timeout (exit 4), never unset or -32004."""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client
from coordinator_core.warm.engine_root import current_engine_clone

_TOKEN = "faketoken"
_KERNEL = Path(__file__).resolve().parents[3] / "coordinator" / "templates" / "bin" / "_machine_local.py"


class _StuckPipe:
    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self):
        threading.Event().wait(5)
        return b""

    def close(self) -> None:
        pass


class _UnsetPipe(_StuckPipe):
    def readline(self):
        result = {"stdout": "", "stderr": "", "exit_code": 1}
        return (json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}) + "\n").encode()


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setenv("MACHINE_LOCAL_DEADLINE_S", "0.05")
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: _TOKEN)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.2)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    svc = breadcrumb.svc_dir(current_engine_clone())
    svc.mkdir(parents=True)
    (svc / breadcrumb.BREADCRUMB_FILENAME).write_text(
        json.dumps({"pipe": "p", "pid": 424242, "stable_pid_start_epoch": 1}), encoding="utf-8"
    )


def _frame(verb: str, entrypoint: str = "machine-local") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "invoke.from_argv",
        "params": {"entrypoint": entrypoint, "argv": [verb, "demo.foo"], "cwd": "."},
    }


def test_stalled_warm_get_is_exit_4_with_the_typed_token(engine, monkeypatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    response = client.try_warm_dispatch(_frame("get"))
    result = response["result"]
    assert result["exit_code"] == 4
    assert result["stderr"].startswith("machine-local-timeout:")
    assert result["stdout"] == ""
    assert "error" not in response


def test_stalled_warm_get_waits_the_kernel_deadline_not_the_mutation_wait(engine, monkeypatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: pytest.fail("mutation wait consulted"))
    assert client.try_warm_dispatch(_frame("get"))["result"]["exit_code"] == 4


def test_an_answered_unset_key_stays_exit_1(engine, monkeypatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda name: _UnsetPipe())
    assert client.try_warm_dispatch(_frame("get"))["result"]["exit_code"] == 1


def test_a_stalled_write_verb_keeps_the_indeterminate_envelope(engine, monkeypatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    response = client.try_warm_dispatch(_frame("set"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE


def test_another_entrypoint_is_untouched(engine, monkeypatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    assert client._machine_local_read_deadline(_frame("get", "cross-repo-memo")) is None


def test_deadline_follows_the_env_and_falls_back_to_the_kernel_backstop(monkeypatch) -> None:
    frame = _frame("get")
    monkeypatch.setenv("MACHINE_LOCAL_DEADLINE_S", "3")
    assert client._machine_local_read_deadline(frame) == 3.0
    for bad in ("junk", "0", "-1", ""):
        monkeypatch.setenv("MACHINE_LOCAL_DEADLINE_S", bad)
        assert client._machine_local_read_deadline(frame) == client._MACHINE_LOCAL_BACKSTOP_S


def test_client_constants_match_the_kernel() -> None:
    spec = importlib.util.spec_from_file_location("_machine_local_pin", _KERNEL)
    kernel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kernel)
    assert client._MACHINE_LOCAL_BACKSTOP_S == kernel._BACKSTOP_S
    assert kernel.EXIT_TIMEOUT == 4


def test_client_read_verbs_match_the_entrypoint() -> None:
    entry = _KERNEL.parents[2] / "bin" / "machine-local.py"
    spec = importlib.util.spec_from_file_location("_machine_local_entry_pin", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert client._MACHINE_LOCAL_READ_VERBS == module._READ_VERBS
