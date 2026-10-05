"""tracked.read_tracked: clean/dirty/racy classification on in-process index bytes."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from coordinator_core.git import git_state
from coordinator_core.git.index_write import _build_entry
from coordinator_core.ops.generator_census import tracked

SHA = "ab" * 20
OLD = 1_700_000_000


def _write_index(repo: Path, names, *, version=2, index_mtime=None):
    entries = b""
    for name in sorted(names):
        st = os.stat(repo / name) if (repo / name).exists() else os.stat(repo)
        entries += _build_entry(name.encode(), 0o100644, SHA, st)
    raw = b"DIRC" + struct.pack(">II", version, len(names)) + entries + b"\x00" * 20
    index = repo / ".git" / "index"
    index.write_bytes(raw)
    if index_mtime is not None:
        os.utime(index, (index_mtime, index_mtime))


def _file(repo: Path, rel: str, text: str = "x = 1\n", mtime=OLD) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")
    os.utime(p, (mtime, mtime))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_clean_entry(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py"], index_mtime=OLD + 1000)
    got = tracked.read_tracked(repo)
    assert got.clean == {"bin/a.py": SHA}
    assert got.dirty == ()
    assert got.scope == ("bin/a.py",)
    assert got.index_source == "index"


def test_size_changed_is_dirty(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py"], index_mtime=OLD + 1000)
    _file(repo, "bin/a.py", "x = 12345\n")
    got = tracked.read_tracked(repo)
    assert got.dirty == ("bin/a.py",)
    assert got.clean == {}


def test_mtime_changed_is_dirty(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py"], index_mtime=OLD + 1000)
    os.utime(repo / "bin/a.py", (OLD + 5, OLD + 5))
    got = tracked.read_tracked(repo)
    assert got.dirty == ("bin/a.py",)


def test_racy_entry_is_dirty(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py"], index_mtime=OLD)
    got = tracked.read_tracked(repo)
    assert got.dirty == ("bin/a.py",)
    assert got.clean == {}


def test_tracked_but_missing_is_skipped(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py", "bin/gone.py"], index_mtime=OLD + 1000)
    got = tracked.read_tracked(repo)
    assert "bin/gone.py" in got.paths
    assert "bin/gone.py" not in got.scope
    assert "bin/gone.py" not in got.dirty
    assert set(got.clean) == {"bin/a.py"}


def test_out_of_scope_excluded(repo):
    names = ["bin/a.py", "bin/a.sh", "docs/x.py", "binary/y.py", "coordinator/bin/z.py",
             "coordinator_core/m.py", "coordinator/other/q.py"]
    for n in names:
        _file(repo, n)
    _write_index(repo, names, index_mtime=OLD + 1000)
    got = tracked.read_tracked(repo)
    assert got.scope == ("bin/a.py", "coordinator/bin/z.py", "coordinator_core/m.py")
    assert set(got.paths) == set(names)


def _v4_index(repo: Path, names):
    out = b""
    prev = b""
    for name in sorted(names):
        b = name.encode()
        fixed = struct.pack(">IIIIIIIIII20sH", 0, 0, OLD, 0, 0, 0, 0o100644, 0, 0, 6,
                            bytes.fromhex(SHA), min(len(b), 0xFFF))
        common = 0
        while common < min(len(prev), len(b)) and prev[common] == b[common]:
            common += 1
        assert len(prev) - common < 128
        out += fixed + bytes([len(prev) - common]) + b[common:] + b"\x00"
        prev = b
    raw = b"DIRC" + struct.pack(">II", 4, len(names)) + out + b"\x00" * 20
    (repo / ".git" / "index").write_bytes(raw)


def test_v4_reaches_fallback_all_dirty(repo):
    names = ["bin/a.py", "coordinator_core/b.py", "docs/c.py"]
    for n in names:
        _file(repo, n)
    _v4_index(repo, names)
    got = tracked.read_tracked(repo)
    assert got.index_source == "fallback"
    assert got.clean == {}
    assert got.scope == ("bin/a.py", "coordinator_core/b.py")
    assert got.dirty == got.scope
    assert set(got.paths) == set(names)


def test_split_index_raises(repo):
    _file(repo, "bin/a.py")
    _write_index(repo, ["bin/a.py"], version=4)
    (repo / ".git" / "sharedindex.0123").write_bytes(b"")
    with pytest.raises(git_state.IndexParseError):
        tracked.read_tracked(repo)


def test_paths_are_posix(repo):
    _file(repo, "coordinator/bin/sub/a.py")
    _write_index(repo, ["coordinator/bin/sub/a.py"], index_mtime=OLD + 1000)
    got = tracked.read_tracked(repo)
    assert got.scope == ("coordinator/bin/sub/a.py",)
    assert all("\\" not in p for p in got.paths + got.scope + got.dirty)
