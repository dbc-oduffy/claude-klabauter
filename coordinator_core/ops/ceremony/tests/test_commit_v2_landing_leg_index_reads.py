"""
coordinator_core.ops.ceremony.tests.test_commit_v2_landing_leg_index_reads

Purpose: the index entries one `ceremony.commit_v2` call walks do not grow with the tracked-file
count when the call hands an untracked path (a wanted path absent from the index must not force a
full-index walk), and the early exit never changes which wanted entries are returned.
"""

from __future__ import annotations

import struct as _struct
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_index
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path: Path, tracked: int) -> Path:
    root = tmp_path / f"repo{tracked}"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    for i in range(tracked):
        f = root / "tracked" / f"d{i % 5}" / f"f{i:05d}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{i}\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


class _CountingStruct:
    """`struct` stand-in that counts the per-entry `>H` flags unpack of `git_index`'s walk."""

    def __init__(self) -> None:
        self.walked = 0

    def unpack(self, fmt, buf):
        if fmt == ">H":
            self.walked += 1
        return _struct.unpack(fmt, buf)

    def __getattr__(self, name):
        return getattr(_struct, name)


def _walked_by_one_call(tmp_path: Path, tracked: int, monkeypatch) -> int:
    root = _repo(tmp_path, tracked)
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    (root / "b.txt").write_text("new\n", encoding="utf-8")
    counter = _CountingStruct()
    monkeypatch.setattr(git_index, "struct", counter)
    reply = commit_v2._handler(
        {"paths": ["a.txt", "b.txt"], "message": "land a and b\n", "session_id": _SID},
        repo_root=root / ".git",
    )
    monkeypatch.undo()
    assert reply["committed"] is True, reply
    return counter.walked


def test_index_entries_walked_per_call_are_equal_at_10_and_2000_tracked_files(tmp_path, monkeypatch):
    small = _walked_by_one_call(tmp_path, 10, monkeypatch)
    large = _walked_by_one_call(tmp_path, 2000, monkeypatch)
    assert small == large, (small, large)


def _write_index(root: Path, names: list) -> None:
    entries = b""
    for name in sorted(names):
        raw = name.encode()
        fixed = _struct.pack(">IIIIIIIIII", 0, 0, 1, 0, 0, 0, 0o100644, 0, 0, 1) + bytes(20)
        fixed += _struct.pack(">H", len(raw))
        entry = fixed + raw + b"\x00"
        entries += entry + b"\x00" * ((8 - len(entry) % 8) % 8)
    (root / ".git" / "index").write_bytes(b"DIRC" + _struct.pack(">II", 2, len(names)) + entries)


def test_an_absent_wanted_path_stops_the_walk_at_its_sort_position_and_loses_no_entry(tmp_path):
    root = tmp_path / "idx"
    (root / ".git").mkdir(parents=True)
    names = ["a.py", "docs/p.md", "state/x.yaml", "z/after.txt", "z/after2.txt"]
    _write_index(root, names)

    got = git_index.parse_index_identity(root, wanted={"a.py", "state/new.yaml", "state/x.yaml"})
    assert sorted(got) == ["a.py", "state/x.yaml"]

    assert sorted(git_index.parse_index_identity(root, wanted={"z/after2.txt", "nope"})) == ["z/after2.txt"]
    assert git_index.parse_index_identity(root, wanted={"zz/never"}) == {}


def test_a_changed_index_is_read_again_not_served_from_an_earlier_call(tmp_path):
    root = tmp_path / "idx2"
    (root / ".git").mkdir(parents=True)
    _write_index(root, ["a.py", "b.py"])
    assert sorted(git_index.parse_index_identity(root, wanted={"a.py", "c.py"})) == ["a.py"]
    _write_index(root, ["a.py", "b.py", "c.py"])
    assert sorted(git_index.parse_index_identity(root, wanted={"a.py", "c.py"})) == ["a.py", "c.py"]
