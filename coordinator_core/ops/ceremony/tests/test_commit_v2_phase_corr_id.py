"""The phase_time row carries the corr_id of the dispatch that ran the handler."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _phase_rows(root: Path) -> list:
    sink = root / ".git" / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    if not sink.exists():
        return []
    rows = [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in rows if r.get("kind") == "phase_time"]


def _bare(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


def test_accessor_is_none_outside_a_dispatch():
    assert ipc.current_corr_id() is None


def test_handler_row_carries_the_ambient_corr_id(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    _bare(tmp_path)
    token = ipc._corr_id_var.set("1234-0-99")
    try:
        commit_v2._handler({"paths": ["a.txt"], "message": ""}, repo_root=tmp_path / ".git")
    finally:
        ipc._corr_id_var.reset(token)
    rows = _phase_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["corr_id"] == "1234-0-99"


def test_row_without_a_dispatch_reads_null(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    _bare(tmp_path)
    commit_v2._handler({"paths": ["a.txt"], "message": ""}, repo_root=tmp_path / ".git")
    rows = _phase_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["corr_id"] is None


def test_row_carries_the_dispatch_op_inside_a_dispatch_and_none_outside(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    _bare(tmp_path)
    token = ipc._dispatch_op_var.set("dispatch.terminal_commit")
    try:
        commit_v2._handler({"paths": ["a.txt"], "message": ""}, repo_root=tmp_path / ".git")
    finally:
        ipc._dispatch_op_var.reset(token)
    commit_v2._handler({"paths": ["a.txt"], "message": ""}, repo_root=tmp_path / ".git")
    rows = _phase_rows(tmp_path)
    assert [r["dispatch_op"] for r in rows] == ["dispatch.terminal_commit", None]


def test_dispatch_message_binds_and_resets_the_dispatch_op(monkeypatch):
    seen = {}

    async def _impl(msg):
        seen["during"] = ipc.current_dispatch_op()
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(ipc, "_dispatch_message_impl", _impl)
    asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "dispatch.x"}, corr_id="1-0-1"))
    assert seen["during"] == "dispatch.x"
    assert ipc.current_dispatch_op() is None


def test_dispatch_message_exposes_and_resets_its_corr_id(monkeypatch):
    seen = {}

    async def _impl(msg):
        seen["during"] = ipc.current_corr_id()
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(ipc, "_dispatch_message_impl", _impl)
    asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "x"}, corr_id="77-0-5"))
    assert seen["during"] == "77-0-5"
    assert ipc.current_corr_id() is None
