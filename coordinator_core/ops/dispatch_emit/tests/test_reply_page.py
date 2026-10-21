"""Tests for dispatch.reply_page."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc
from coordinator_core.ops.dispatch_emit import reply_page
from coordinator_core.ops.dispatch_emit import reply_sidecar as rs


def _manifest(rows: int = 40) -> dict:
    return {
        "rows": [{"id": f"C{i}", "title": f"row {i} café " + "x" * 80, "writes": [f"src/f{i}.py"]} for i in range(rows)],
        "head_sha": "a" * 40,
    }


@pytest.fixture
def sidecar(tmp_path):
    (tmp_path / ".git").mkdir()
    path = tmp_path / "state" / "warp" / "run1" / "manifest.json"
    handle = rs.write_reply(tmp_path, path, _manifest())
    return tmp_path, path, handle


def _join(root, handle):
    first = reply_page._handler({"handle": handle}, repo_root=root)
    chunks = [first["chunk"]]
    for i in range(1, first["pages"]):
        chunks.append(reply_page._handler({"handle": handle, "page": i}, repo_root=root)["chunk"])
    return "".join(chunks), first


def test_pages_of_forty_row_manifest_join_to_sidecar_text(sidecar):
    root, path, handle = sidecar
    text, first = _join(root, handle)
    assert first["pages"] > 1
    assert text == path.read_bytes().decode("latin-1")
    assert json.loads(text) == _manifest()
    assert first["sha256"] == handle["sha256"] and first["bytes"] == handle["bytes"]


def test_tampered_sidecar_is_refused(sidecar):
    root, path, handle = sidecar
    path.write_bytes(path.read_bytes().replace(b"row 3 ", b"row X ", 1))
    reply = reply_page._handler({"handle": handle}, repo_root=root)
    assert set(reply) == {"error"}
    assert reply["error"].startswith(gcc.RELAY_CORRUPT_HEAD)


def test_path_outside_sidecar_roots_is_refused(sidecar):
    root, _, handle = sidecar
    (root / "docs").mkdir()
    (root / "docs" / "x.json").write_bytes(b"{}")
    bad = {**handle, "path": "docs/x.json"}
    reply = reply_page._handler({"handle": bad}, repo_root=root)
    assert reply["error"].startswith(gcc.RELAY_CORRUPT_HEAD)


def test_out_of_range_and_malformed_page_reply_error(sidecar):
    root, _, handle = sidecar
    assert "error" in reply_page._handler({"handle": handle, "page": 999}, repo_root=root)
    assert "error" in reply_page._handler({"handle": handle, "page": -1}, repo_root=root)
    assert "error" in reply_page._handler({"handle": handle, "page": True}, repo_root=root)
    assert "error" in reply_page._handler({}, repo_root=root)


def test_page_call_is_under_200ms_process_time_and_spawns_nothing(sidecar, monkeypatch):
    root, _, handle = sidecar

    def _no_spawn(*a, **k):
        raise AssertionError("dispatch.reply_page spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    start = time.process_time()
    reply = reply_page._handler({"handle": handle, "page": 0}, repo_root=root)
    elapsed = time.process_time() - start
    assert "chunk" in reply
    assert elapsed < 0.2
