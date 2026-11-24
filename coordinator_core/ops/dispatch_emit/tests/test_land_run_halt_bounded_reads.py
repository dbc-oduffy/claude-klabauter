"""Halt-path file reads (`_superseded_candidates` run.json, `_differs` stat + blob hash) are bounded:
a stalled read raises `IoIndeterminate` out of the helper instead of blocking, and every
non-stalled answer is unchanged."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from coordinator_core import bounded_io
from coordinator_core.git import call_io_scope, commit
from coordinator_core.ops.dispatch_emit import land_run_halt

SIZING = "state/sizings/2026-10-31-a6c83f-2-hang.yaml"
RUN_ID = "ask-20261101T000000-aaaaaa"
DEADLINE = 0.05


def _ask_dir(root: Path, name: str, *, run_json: bool = True) -> None:
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    if run_json:
        (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def stall():
    """An Event a stalled read waits on; released at teardown so the leaked worker returns."""
    release = threading.Event()
    yield release
    release.set()


def test_stalled_run_json_read_raises_indeterminate(tmp_path, monkeypatch, stall) -> None:
    _ask_dir(tmp_path, "ask-20261031T100000-bbbbbb")
    real = bounded_io._read_registered
    monkeypatch.setattr(
        bounded_io, "_read_registered",
        lambda p: stall.wait(10) and None if p.endswith("run.json") else real(p),
    )
    with call_io_scope.bounded_reads(DEADLINE), pytest.raises(bounded_io.IoIndeterminate) as caught:
        land_run_halt._superseded_candidates(tmp_path, RUN_ID, SIZING)
    assert caught.value.path.endswith("run.json")
    assert not isinstance(caught.value, OSError)


def test_absent_run_json_is_skipped_and_present_one_is_kept(tmp_path) -> None:
    _ask_dir(tmp_path, "ask-20261031T100000-bbbbbb", run_json=False)
    _ask_dir(tmp_path, "ask-20261031T120000-dddddd")
    with call_io_scope.bounded_reads(DEADLINE):
        found = land_run_halt._superseded_candidates(tmp_path, RUN_ID, SIZING)
    assert found == ["ask-20261031T120000-dddddd"]


def test_stalled_blob_hash_raises_indeterminate(tmp_path, monkeypatch, stall) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: stall.wait(10) and None)
    with call_io_scope.bounded_reads(DEADLINE), pytest.raises(bounded_io.IoIndeterminate) as caught:
        land_run_halt._differs(tmp_path, {"a.txt": (0o100644, _blob(b"hello\n"))}, ["a.txt"])
    assert caught.value.path.endswith("a.txt")


def test_stalled_stat_raises_indeterminate_even_for_a_path_the_tree_lacks(tmp_path, monkeypatch, stall) -> None:
    (tmp_path / "new.txt").write_bytes(b"x")
    monkeypatch.setattr(land_run_halt, "_stat_and_blob", lambda root, rel, want: stall.wait(10) and (False, None))
    with call_io_scope.bounded_reads(DEADLINE), pytest.raises(bounded_io.IoIndeterminate):
        land_run_halt._differs(tmp_path, {}, ["new.txt"])


def test_differs_answers_unchanged_without_a_stall(tmp_path) -> None:
    (tmp_path / "same.txt").write_bytes(b"hello\n")
    (tmp_path / "edited.txt").write_bytes(b"new\n")
    (tmp_path / "fresh.txt").write_bytes(b"f\n")
    (tmp_path / "d").mkdir()
    entries = {
        "same.txt": (0o100644, _blob(b"hello\n")),
        "edited.txt": (0o100644, _blob(b"old\n")),
        "deleted.txt": (0o100644, _blob(b"x")),
        "d": (0o100644, _blob(b"x")),
    }
    rels = ["same.txt", "edited.txt", "fresh.txt", "deleted.txt", "d", "never.txt"]
    with call_io_scope.bounded_reads(DEADLINE):
        differ, gone = land_run_halt._differs(tmp_path, entries, rels)
    assert differ == {"edited.txt", "fresh.txt"}
    assert gone == {"deleted.txt", "d"}


def test_differs_stats_each_path_once(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    calls: list = []
    real = land_run_halt._stat_and_blob
    monkeypatch.setattr(land_run_halt, "_stat_and_blob", lambda *a: calls.append(a[1]) or real(*a))
    land_run_halt._differs(tmp_path, {"a.txt": (0o100644, _blob(b"hello\n"))}, ["a.txt", "b.txt"])
    assert calls == ["a.txt", "b.txt"]


def test_a_stall_mid_list_names_the_stalled_path_and_a_long_list_uses_one_worker(tmp_path, monkeypatch, stall) -> None:
    rels = [f"f{i}.txt" for i in range(300)]
    for rel in rels:
        (tmp_path / rel).write_bytes(b"x")
    real = land_run_halt._stat_and_blob
    started: list = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k.get("name")) or _Real(*a, **k))
    with call_io_scope.bounded_reads(DEADLINE):
        differ, _ = land_run_halt._differs(tmp_path, {}, rels)
    assert len(differ) == 300 and started == ["halt-watched-reads"]
    monkeypatch.setattr(
        land_run_halt, "_stat_and_blob",
        lambda root, rel, want: stall.wait(10) and (False, None) if rel == "f150.txt" else real(root, rel, want),
    )
    with call_io_scope.bounded_reads(DEADLINE), pytest.raises(bounded_io.IoIndeterminate) as caught:
        land_run_halt._differs(tmp_path, {}, rels)
    assert caught.value.path.endswith("f150.txt")


_Real = threading.Thread
