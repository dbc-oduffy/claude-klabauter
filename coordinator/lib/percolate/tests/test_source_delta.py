from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.git_state import format_publish_trailers  # noqa: E402
from percolate.publish_delta import ColdReason  # noqa: E402
from percolate.source_delta import SourceDelta, source_delta  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NC = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
SIG = "a" * 64


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NC
    ).stdout.strip()


def _repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _commit(root, files, msg, delete=()):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for rel in delete:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _blob(root, rev, rel):
    return _git(root, "rev-parse", f"{rev}:{rel}")


@pytest.fixture
def world(tmp_path):
    src = _repo(tmp_path, "src")
    base = _commit(
        src,
        {
            "a.txt": b"a\n",
            "d/keep.txt": b"keep\n",
            "d/mod.txt": b"old\n",
            "d/gone.txt": b"gone\n",
            "e/old_name.txt": b"moved\n",
            "x.sh": b"#!/bin/sh\n",
        },
        "base",
    )
    dest = _repo(tmp_path, "dest")
    msg = "publish\n\n" + format_publish_trailers(
        source_head=base, signature=SIG, round_id="r1"
    )
    _commit(dest, {"f": b"1"}, msg)
    return src, dest, base


def test_changed_deleted_and_unchanged(world):
    src, dest, base = world
    head = _commit(
        src,
        {"d/mod.txt": b"new\n", "d/added.txt": b"add\n", "e/new_name.txt": b"moved\n"},
        "next",
        delete=["d/gone.txt", "e/old_name.txt"],
    )
    sd = source_delta(dest, src, head)
    assert isinstance(sd, SourceDelta)
    assert sd.base_sha == base and sd.head_sha == head
    assert sd.changed == {
        "d/mod.txt": _blob(src, head, "d/mod.txt"),
        "d/added.txt": _blob(src, head, "d/added.txt"),
        "e/new_name.txt": _blob(src, head, "e/new_name.txt"),
    }
    assert sd.deleted == frozenset({"d/gone.txt", "e/old_name.txt"})
    assert sd.unchanged_blob("a.txt") == _blob(src, head, "a.txt")
    assert sd.unchanged_blob("d/keep.txt") == _blob(src, head, "d/keep.txt")
    assert sd.unchanged_blob("d/mod.txt") is None
    assert sd.unchanged_blob("d/gone.txt") is None
    assert sd.unchanged_blob("d/missing.txt") is None
    assert sd.unchanged_blob("d") is None
    assert sd.unchanged_blob("a.txt/under") is None


def test_mode_flip_is_changed(world):
    src, dest, base = world
    _git(src, "update-index", "--chmod=+x", "a.txt")
    _git(src, "commit", "-q", "-m", "chmod")
    head = _git(src, "rev-parse", "HEAD")
    sd = source_delta(dest, src, head)
    assert isinstance(sd, SourceDelta)
    assert set(sd.changed) == {"a.txt"}
    assert sd.unchanged_blob("a.txt") is None


def test_same_sha_is_empty_delta(world):
    src, dest, base = world
    sd = source_delta(dest, src, base)
    assert isinstance(sd, SourceDelta)
    assert sd.changed == {} and sd.deleted == frozenset()
    assert sd.unchanged_blob("d/keep.txt") == _blob(src, base, "d/keep.txt")


def test_no_stamp_is_cold(tmp_path, world):
    src, _, base = world
    empty = _repo(tmp_path, "empty")
    r = source_delta(empty, src, base)
    assert isinstance(r, ColdReason) and r.code == "no-stamp"


def test_foreign_dest_commit_is_cold(tmp_path, world):
    src, _, base = world
    dest2 = _repo(tmp_path, "dest2")
    _commit(dest2, {"f": b"1"}, "hand edit")
    r = source_delta(dest2, src, base)
    assert isinstance(r, ColdReason) and r.code == "foreign-commit"


def test_unreadable_base_is_cold_not_raised(tmp_path, world):
    src, _, base = world
    dest3 = _repo(tmp_path, "dest3")
    ghost = "b" * 40
    msg = "publish\n\n" + format_publish_trailers(source_head=ghost, signature=SIG, round_id="r1")
    _commit(dest3, {"f": b"1"}, msg)
    r = source_delta(dest3, src, base)
    assert isinstance(r, ColdReason) and r.code == "object-unreadable"


def test_unreadable_pinned_is_cold(world):
    src, dest, _ = world
    r = source_delta(dest, src, "c" * 40)
    assert isinstance(r, ColdReason) and r.code == "object-unreadable"


def test_zero_spawns(world, monkeypatch):
    src, dest, base = world
    head = _commit(src, {"d/mod.txt": b"new\n"}, "next")
    calls = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        calls.append(a)
        real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    sd = source_delta(dest, src, head)
    assert isinstance(sd, SourceDelta)
    sd.unchanged_blob("a.txt")
    assert calls == []
