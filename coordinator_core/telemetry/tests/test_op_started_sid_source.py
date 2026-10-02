"""record_op_started carries sid_source, and dispatch_message derives it from
where the session id came from."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from coordinator_core import ipc
from coordinator_core.session import core
from coordinator_core.telemetry.op_latency import record_op_started


async def _handler(params, ctx=None, repo_root=None):
    return {"ok": True}


def _started_rows(sink):
    rows = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r.get("kind") == "started"]


@pytest.fixture()
def sink(tmp_path, monkeypatch):
    common = tmp_path / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common)
    return common / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _dispatch(tmp_path):
    method = "test.sid_source"
    ipc._REGISTRY[method] = _handler
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {},
               "_origin_worktree": str(tmp_path)}
        asyncio.run(ipc.dispatch_message(msg))
    finally:
        ipc._REGISTRY.pop(method, None)


def test_record_op_started_writes_sid_source(tmp_path, sink):
    record_op_started(op="p", t_start=1.0, corr_id="c", repo_root=tmp_path,
                      sid="s", sid_source="env")
    assert _started_rows(sink)[0]["sid_source"] == "env"


def test_record_op_started_sid_source_defaults_to_null(tmp_path, sink):
    record_op_started(op="p", t_start=1.0, corr_id="c", repo_root=tmp_path)
    assert _started_rows(sink)[0]["sid_source"] is None


def test_dispatch_labels_a_carried_identity(tmp_path, sink):
    with core.session_identity_override(str(uuid.uuid4())):
        _dispatch(tmp_path)
    assert _started_rows(sink)[0]["sid_source"] == "carried"


def test_dispatch_labels_a_warm_request_without_identity_spawner_env(tmp_path, sink):
    with core.warm_served_request():
        _dispatch(tmp_path)
    assert _started_rows(sink)[0]["sid_source"] == "spawner-env"


def test_dispatch_labels_a_cold_invocation_env(tmp_path, sink):
    _dispatch(tmp_path)
    assert _started_rows(sink)[0]["sid_source"] == "env"
