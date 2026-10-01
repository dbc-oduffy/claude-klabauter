"""Pins the fanout.* op registrations: handler resolution, zero-spawn/no-I/O, and the brightline CPU budget."""

from __future__ import annotations

import builtins
import copy
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.fanout import contract

FIX = Path(__file__).parent / "fixtures"
OPS = ("fanout.compose", "fanout.census", "fanout.reconcile")


def _manifest(n: int = 50) -> dict:
    m = contract.parse_manifest_yaml((FIX / "manifest-minimal.yaml").read_text(encoding="utf-8"))
    base = m["workers"][0]
    m["workers"] = []
    for i in range(n):
        w = copy.deepcopy(base)
        w["id"] = f"worker-{i}"
        w["focus"] = f"repos.worker_{i}"
        m["workers"].append(w)
    return m


def _params(op: str, manifest: dict) -> dict:
    sessions = json.loads((FIX / "list-sessions.json").read_text(encoding="utf-8"))
    details = json.loads((FIX / "get-session.json").read_text(encoding="utf-8"))
    if op == "fanout.compose":
        return {"manifest": manifest}
    census_params = {
        "manifest": manifest,
        "sessions": sessions,
        "session_details": details,
        "checkin_comments": [],
    }
    if op == "fanout.census":
        return census_params
    handler = get_op_handler("fanout.census")
    return {"manifest": manifest, "census": handler(census_params), "pause": {"message": "hold"}}


def test_all_three_ops_resolve() -> None:
    for op in OPS:
        assert callable(get_op_handler(op)), op


def test_invalid_manifest_returns_error() -> None:
    result = get_op_handler("fanout.compose")({"manifest": {"schema": "nope"}})
    assert "error" in result


@pytest.mark.parametrize("op", OPS)
def test_zero_spawn_no_io_and_cpu_budget(op: str, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    contract.validate_manifest(manifest)
    params = _params(op, manifest)
    handler = get_op_handler(op)

    def boom(*_a, **_k):
        raise AssertionError("forbidden I/O or spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "posix_spawn", boom, raising=False)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(builtins, "open", boom)

    start = time.process_time()
    result = handler(params)
    elapsed = time.process_time() - start

    assert "error" not in result, result
    assert elapsed < 0.5
