"""Pins kept.json's post_checkpoint record and its spawn-free helpers."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import kept_work as k
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _w(root, rel, data=b"x\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return blob_sha(data)


def test_hunks_filter_arms(tmp_path):
    keep = _w(tmp_path, "a.py")
    _w(tmp_path, "landed.py")
    _w(tmp_path, "rec.py")
    _w(tmp_path, "scratch/s.py")
    out = k.post_checkpoint_hunks(
        tmp_path,
        ["a.py", "a.py", "landed.py", "rec.py", "scratch/s.py", "gone.py", "../x", "/abs", "C" + ":/x", "", ".\\a.py"],
        landed=["landed.py"],
        recorded=["rec.py"],
    )
    assert out == {"a.py": keep}


def test_hunks_baseline_drops_unwritten(tmp_path):
    same = _w(tmp_path, "same.py")
    _w(tmp_path, "new.py", b"new\n")
    out = k.post_checkpoint_hunks(
        tmp_path, ["same.py", "new.py"], landed=[], recorded=[], baseline={"same.py": same, "new.py": None}
    )
    assert list(out) == ["new.py"]


def test_hunks_declared_order(tmp_path):
    _w(tmp_path, "b.py")
    _w(tmp_path, "a.py")
    assert list(k.post_checkpoint_hunks(tmp_path, ["b.py", "a.py"], landed=[], recorded=[])) == ["b.py", "a.py"]


def test_round_trip_and_byte_identity(tmp_path):
    chunks = {"C1": {"paths": {"a": "x"}}}
    plain = tmp_path / "plain"
    k.write(plain, chunks, None)
    assert "post_checkpoint" not in json.loads((plain / k.KEPT_FILE).read_text())
    assert k.write(tmp_path / "e", {}, None, {}) is None
    path = k.write(tmp_path / "p", {}, None, {"z": "1", "a": "2"})
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert k.read(tmp_path / "p")["post_checkpoint"] == {"z": "1", "a": "2"}
    assert raw.index(b'"a"') < raw.index(b'"z"')


@pytest.mark.parametrize("bad", [["x"], "s", 3, {"a": 1, "b": "ok", "c": None}])
def test_malformed_entries_dropped(tmp_path, bad):
    (tmp_path / k.KEPT_FILE).write_text(json.dumps({"chunks": {}, "post_checkpoint": bad}), encoding="utf-8")
    got = k.read(tmp_path)["post_checkpoint"]
    assert got == ({"b": "ok"} if isinstance(bad, dict) else {})


def test_holding_and_drifted(tmp_path):
    rec = tmp_path / "rec"
    ok = _w(tmp_path, "ok.py")
    _w(tmp_path, "moved.py", b"old\n")
    want = blob_sha(b"old\n")
    k.write(rec, {}, None, {"ok.py": ok, "moved.py": want, "gone.py": "f" * 40})
    _w(tmp_path, "moved.py", b"new\n")
    holding, drifted = k.kept_post_checkpoint(tmp_path, rec)
    assert holding == {"ok.py": ok}
    assert drifted == ["gone.py", "moved.py"]


def test_fingerprint_union(tmp_path):
    k.write(tmp_path, {"C1": {"paths": {"a.py": "b1"}}, "C2": {"paths": {"a.py": "b2", "c.py": "b3"}}}, None, {"a.py": "b4", "p.py": "b5"})
    assert k.kept_fingerprints(tmp_path) == {
        "a.py": frozenset({"b1", "b2", "b4"}),
        "c.py": frozenset({"b3"}),
        "p.py": frozenset({"b5"}),
    }
    assert k.kept_fingerprints(tmp_path / "none") == {}
