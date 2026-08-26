"""Tests for `coordinator_core.git.git_index`.

Fixture index files are SYNTHESISED byte-for-byte, following the same
convention `test_git_state.py` documents: this box's own index never
carries a v4 file, so that shape must be built directly rather than
harvested. `_build_index` here is a NARROWER re-derivation scoped to this
module's own (mode, size, mtime) fields -- not imported from
`test_git_state.py`, so a bug shared by both fixture builders would not
cancel out silently.

Nothing in this file spawns a process.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_index import (  # noqa: E402
    IndexParseError,
    IndexV4Unsupported,
    parse_index_stat,
    scoped_status,
)

_SIGNATURE = b"DIRC"
_ZERO_SHA = bytes(20)


def _entry_fixed(
    mode: int, size: int, mtime: int, name_len: int, extended: bool, mtime_nsec: int = 0
) -> bytes:
    fixed = struct.pack(
        ">IIIIIIIIII",
        0, 0,               # ctime s/ns
        mtime, mtime_nsec,  # mtime s/ns
        0,          # dev
        0,          # ino
        mode,
        0,          # uid
        0,          # gid
        size,
    )
    fixed += _ZERO_SHA
    flags = min(name_len, 0x0FFF)
    if extended:
        flags |= 0x4000
    fixed += struct.pack(">H", flags)
    if extended:
        fixed += struct.pack(">H", 0)
    return fixed


def _build_index(entries, *, version=2):
    """`entries`: list of dicts with keys mode(int), size(int), mtime(int),
    name(str), extended(bool, default False), mtime_nsec(int, default 0)."""
    out = _SIGNATURE + struct.pack(">II", version, len(entries))
    for e in entries:
        name = e["name"].encode("utf-8")
        fixed = _entry_fixed(
            e["mode"],
            e["size"],
            e["mtime"],
            len(name),
            e.get("extended", False),
            e.get("mtime_nsec", 0),
        )
        entry_len = len(fixed) + len(name) + 1
        padding = (8 - (entry_len % 8)) % 8
        out += fixed + name + b"\x00" + (b"\x00" * padding)
    out += b"\x00" * 20  # fake trailing checksum, never verified
    return out


def _build_v4_index(entries):
    """A minimal, structurally-plausible v4 index -- this module must
    refuse it on the version field alone, before ever reaching a
    prefix-compressed name, so the entry bytes here need not be valid v4
    encoding."""
    out = _SIGNATURE + struct.pack(">II", 4, len(entries))
    for e in entries:
        name = e["name"].encode("utf-8")
        fixed = _entry_fixed(e["mode"], e["size"], e["mtime"], len(name), False)
        out += fixed + b"\x00" + name + b"\x00"
    out += b"\x00" * 20
    return out


def _write_index(gitdir: Path, raw: bytes) -> Path:
    gitdir.mkdir(parents=True, exist_ok=True)
    path = gitdir / "index"
    path.write_bytes(raw)
    return path


def _plain_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path




def test_v4_index_refuses(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_v4_index([{"name": "a.txt", "mode": 0o100644, "size": 3, "mtime": 100}])
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexV4Unsupported):
        parse_index_stat(repo)


def test_stat_matching_file_reads_clean_without_reading_bytes(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "clean.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index(
        [{"name": "clean.txt", "mode": 0o100644, "size": st.st_size, "mtime": int(st.st_mtime)}]
    )
    _write_index(repo / ".git", raw)

    # Verify the module never opens the file for reading: monkeypatch
    # read_bytes on Path to explode, then confirm the verdict still comes
    # back clean (only the index and os.stat were touched).
    orig_read_bytes = Path.read_bytes

    def _guard(self, *a, **kw):
        if self.name == "clean.txt":
            raise AssertionError("scoped_status must not read file bytes")
        return orig_read_bytes(self, *a, **kw)

    Path.read_bytes = _guard
    try:
        verdicts = scoped_status(repo, ["clean.txt"])
    finally:
        Path.read_bytes = orig_read_bytes

    assert verdicts == {"clean.txt": "clean"}


def test_size_only_change_reads_candidate(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "grew.txt"
    f.write_bytes(b"abcdef")
    st = f.stat()
    raw = _build_index(
        [{"name": "grew.txt", "mode": 0o100644, "size": 3, "mtime": int(st.st_mtime)}]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["grew.txt"])
    assert verdicts == {"grew.txt": "candidate"}


def test_mtime_only_change_reads_candidate(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "touched.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index(
        [{
            "name": "touched.txt",
            "mode": 0o100644,
            "size": st.st_size,
            "mtime": int(st.st_mtime) - 1000,
        }]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["touched.txt"])
    assert verdicts == {"touched.txt": "candidate"}


def test_deleted_path_reads_deleted(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"name": "gone.txt", "mode": 0o100644, "size": 3, "mtime": 100}]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["gone.txt"])
    assert verdicts == {"gone.txt": "deleted"}


def test_untracked_path_reads_untracked(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([])
    _write_index(repo / ".git", raw)
    (repo / "new.txt").write_bytes(b"hi")

    verdicts = scoped_status(repo, ["new.txt"])
    assert verdicts == {"new.txt": "untracked"}


def test_same_size_rewrite_inside_the_same_mtime_second_reads_candidate(tmp_path):
    """The racily-clean hole, in the exact shape the archival path hits it:
    a same-size rewrite landing inside the index entry's own mtime-second.
    `(size, mtime_sec)` still matches -- only the sub-second half of the
    index's mtime field separates the two.
    """
    repo = _plain_repo(tmp_path)
    f = repo / "racy.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index([{
        "name": "racy.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": st.st_mtime_ns % 1_000_000_000,
    }])
    _write_index(repo / ".git", raw)

    # Same second, same size, different bytes and a moved sub-second mtime.
    f.write_bytes(b"xyz")
    os.utime(f, ns=(st.st_atime_ns, (int(st.st_mtime) * 1_000_000_000) + 123_456_789))

    assert scoped_status(repo, ["racy.txt"]) == {"racy.txt": "candidate"}


def test_matching_sub_second_mtime_still_reads_clean(tmp_path):
    """The nanosecond compare must not swallow the fast path whole: an
    untouched file whose stored `mtime_nsec` still matches reads clean.
    """
    repo = _plain_repo(tmp_path)
    f = repo / "settled.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index([{
        "name": "settled.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": st.st_mtime_ns % 1_000_000_000,
    }])
    _write_index(repo / ".git", raw)

    assert scoped_status(repo, ["settled.txt"]) == {"settled.txt": "clean"}


def test_zero_stored_nsec_falls_back_to_the_second_compare(tmp_path):
    """A git that recorded no sub-second stat writes `0`. Comparing that
    against a real worktree nanosecond would read every clean path as
    `candidate`, which `git_native` maps to worktree divergence and fails
    the commit loud -- so a zero must fall back, not tighten.
    """
    repo = _plain_repo(tmp_path)
    f = repo / "nonsec.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    assert st.st_mtime_ns % 1_000_000_000 != 0, "fixture needs a sub-second mtime on disk"
    raw = _build_index([{
        "name": "nonsec.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": 0,
    }])
    _write_index(repo / ".git", raw)

    assert scoped_status(repo, ["nonsec.txt"]) == {"nonsec.txt": "clean"}


def test_absent_index_file_returns_empty(tmp_path):
    repo = _plain_repo(tmp_path)
    assert parse_index_stat(repo) == {}


def test_bad_signature_raises(tmp_path):
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", b"NOPE" + struct.pack(">II", 2, 0) + b"\x00" * 20)

    with pytest.raises(IndexParseError):
        parse_index_stat(repo)
