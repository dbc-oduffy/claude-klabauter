"""A warm op still inside the cold import of its module when the caller gives up is retry-safe.

The worker marks the import phase in the ack journal; the caller's -32004 fences the worker out of
the handler and reports `retry_safe` true. A warm module, or a worker already past import, keeps
`retry_safe` false.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, dispatch_ack, server

_OP = "ceremony.scoped_git_commit"
_MSG = {"jsonrpc": "2.0", "id": 1, "method": _OP, "params": {}}


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda engine_root=None: tmp_path)
    monkeypatch.setattr("coordinator_core.warm.engine_root.current_engine_clone", lambda: tmp_path)
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "_delivering_engine", lambda: None)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    return dispatch_ack.journal_dir_under(tmp_path)


class _WorkerPipe:
    """Stands in for the engine: on delivery it plays `worker`, then never answers."""

    def __init__(self, worker):
        self._worker = worker
        self.key = None

    def write(self, data: bytes) -> None:
        self.key = json.loads(data)["_dispatch_key"]
        self._worker(self.key)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        threading.Event().wait(30)
        return b""

    def close(self) -> None:
        pass


def _dispatch(monkeypatch: pytest.MonkeyPatch, worker) -> dict:
    pipe = _WorkerPipe(worker)
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipe)
    response = client.try_warm_dispatch(dict(_MSG))
    assert response is not None
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    return response["error"]


def test_op_still_in_module_import_replies_retry_safe(journal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    markers: dict = {}

    def worker(key: str) -> None:
        markers[key] = dispatch_ack.begin_import_phase(journal, key)

    error = _dispatch(monkeypatch, worker)
    assert error["data"]["retry_safe"] is True
    assert error["data"]["import_phase"] is True
    (marker,) = markers.values()
    assert marker is not None
    assert dispatch_ack.end_import_phase(marker) is False


def test_op_past_module_import_stays_not_retry_safe(journal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def worker(key: str) -> None:
        assert dispatch_ack.end_import_phase(dispatch_ack.begin_import_phase(journal, key)) is True

    error = _dispatch(monkeypatch, worker)
    assert error["data"]["retry_safe"] is False
    assert "import_phase" not in error["data"]


def test_op_with_a_warm_module_stays_not_retry_safe(journal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    error = _dispatch(monkeypatch, lambda key: None)
    assert error["data"]["retry_safe"] is False


def test_worker_imports_a_cold_op_module_under_a_marker(
    journal: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    monkeypatch.setitem(OP_MODULE_MAP, "test.cold_op", "colorsys")
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    claim_dir = str(tmp_path / "dispatch-claims")
    msg = {"method": "test.cold_op"}
    assert server._import_op_module(msg, claim_dir, "9-9") is True
    assert "colorsys" in sys.modules
    assert dispatch_ack.abandon_import_phase(journal, "9-9") is False


def test_worker_fenced_mid_import_does_not_start_the_handler(
    journal: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    monkeypatch.setitem(OP_MODULE_MAP, "test.cold_op", "colorsys")
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    fenced: list = []

    def import_while_caller_gives_up(name: str):
        fenced.append(dispatch_ack.abandon_import_phase(journal, "9-9"))

    monkeypatch.setattr(importlib, "import_module", import_while_caller_gives_up)
    claim_dir = str(tmp_path / "dispatch-claims")
    assert server._import_op_module({"method": "test.cold_op"}, claim_dir, "9-9") is False
    assert fenced == [True]


def test_a_warm_module_writes_no_marker(journal: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    monkeypatch.setitem(OP_MODULE_MAP, "test.warm_op", "json")
    assert server._import_op_module({"method": "test.warm_op"}, str(tmp_path / "dispatch-claims"), "9-9") is True
    assert not Path(journal).exists()
