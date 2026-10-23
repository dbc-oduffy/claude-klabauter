"""Tests for coordinator_core.ops.review_reverify_probe (open/close/crash/refusal, zero spawns)."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import review_reverify_probe as probe


@pytest.fixture
def world(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    root = tmp_path / "repo"
    snap = tmp_path / "share" / "x.pre-review"
    (root / "pkg").mkdir(parents=True)
    snap.mkdir(parents=True)
    (root / "pkg" / "a.py").write_bytes(b"post-a\r\n")
    (root / "pkg" / "b.py").write_bytes(b"post-b\n")
    return root, snap


WRITES = {"pkg/a.py": b"probe-a\r\n", "pkg/b.py": b"probe-b\n"}


def test_open_writes_probe_and_stashes_post(world):
    root, snap = world
    out = probe.open_probe(root, snap, WRITES, ["t::x"])
    assert out["error"] is None and out["probed"] == ["pkg/a.py", "pkg/b.py"]
    assert (root / "pkg" / "a.py").read_bytes() == b"probe-a\r\n"
    assert probe.is_open(snap)
    rec = json.loads((snap / "probe.json").read_text())
    stash = snap / "probe" / rec["files"]["pkg/a.py"]["post"]
    assert stash.read_bytes() == b"post-a\r\n"


def test_close_restores_bytes_and_records_rerun(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::x"])
    assert probe.closed_rerun(snap) == set()
    out = probe.close_probe(root, snap)
    assert out["error"] is None and out["rerun"] == ["t::x"]
    assert (root / "pkg" / "a.py").read_bytes() == b"post-a\r\n"
    assert (root / "pkg" / "b.py").read_bytes() == b"post-b\n"
    assert not probe.is_open(snap)
    assert probe.closed_rerun(snap) == {"t::x"}


def test_second_open_refuses_and_writes_nothing(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::x"])
    before = (snap / "probe.json").read_bytes()
    out = probe.open_probe(root, snap, {"pkg/a.py": b"other"}, ["t::y"])
    assert out["error"] and out["probed"] == []
    assert (snap / "probe.json").read_bytes() == before
    assert (root / "pkg" / "a.py").read_bytes() == b"probe-a\r\n"


def test_close_after_third_party_edit_refuses_and_names_path(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::x"])
    (root / "pkg" / "b.py").write_bytes(b"third party")
    out = probe.close_probe(root, snap)
    assert out["error"] and "pkg/b.py" in out["error"]
    assert (root / "pkg" / "a.py").read_bytes() == b"probe-a\r\n"
    assert probe.is_open(snap) and not (snap / "probe-closed.json").exists()


def test_crash_half_written_tree_closes_cleanly(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::x"])
    (root / "pkg" / "b.py").write_bytes(b"post-b\n")
    out = probe.close_probe(root, snap)
    assert out["error"] is None and out["restored"] == ["pkg/a.py"]
    assert (root / "pkg" / "a.py").read_bytes() == b"post-a\r\n"
    assert probe.closed_rerun(snap) == {"t::x"}


def test_close_without_probe_is_noop(world):
    root, snap = world
    assert probe.close_probe(root, snap) == {"restored": [], "rerun": [], "error": None}
    assert not (snap / "probe-closed.json").exists()


def test_second_close_is_noop(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::x"])
    probe.close_probe(root, snap)
    assert probe.close_probe(root, snap)["restored"] == []
    assert probe.closed_rerun(snap) == {"t::x"}


def test_closed_rerun_empty_without_record(world):
    _, snap = world
    assert probe.closed_rerun(snap) == set()


def test_reopen_deletes_previous_closed_record(world):
    root, snap = world
    probe.open_probe(root, snap, WRITES, ["t::old"])
    probe.close_probe(root, snap)
    assert probe.closed_rerun(snap) == {"t::old"}
    probe.open_probe(root, snap, WRITES, ["t::new"])
    assert probe.closed_rerun(snap) == set()
    probe.close_probe(root, snap)
    assert probe.closed_rerun(snap) == {"t::new"}


def test_escaping_path_refuses_nothing_written(world):
    root, snap = world
    out = probe.open_probe(root, snap, {"pkg/a.py": b"x", "../evil": b"y"}, [])
    assert out["error"] and not probe.is_open(snap)
    assert (root / "pkg" / "a.py").read_bytes() == b"post-a\r\n"
