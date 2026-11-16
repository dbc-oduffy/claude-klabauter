"""checkin_blob_sha adds the bytes it reads to the bound byte counter and nothing else changes."""

from __future__ import annotations

import hashlib
import subprocess

from coordinator_core.git import commit

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True, check=True,
                   creationflags=_NO_WINDOW)
    return tmp_path


def test_unbound_counter_changes_nothing(tmp_path):
    root = _repo(tmp_path)
    (root / "f.txt").write_bytes(b"hello\n")
    expected = hashlib.sha1(b"blob 6\0hello\n").hexdigest()
    assert commit.checkin_blob_sha(root, "f.txt") == expected
    assert commit.bytes_read_total() == 0


def test_bound_counter_accumulates_len_of_bytes_read(tmp_path):
    root = _repo(tmp_path)
    (root / "a.txt").write_bytes(b"a" * 40)
    (root / "b.txt").write_bytes(b"b" * 2)
    token = commit.bind_byte_counter()
    try:
        commit.checkin_blob_sha(root, "a.txt")
        commit.checkin_blob_sha(root, "b.txt")
        assert commit.bytes_read_total() == 42
    finally:
        commit.reset_byte_counter(token)
    assert commit.bytes_read_total() == 0


def test_unreadable_file_adds_nothing_and_returns_none(tmp_path):
    root = _repo(tmp_path)
    token = commit.bind_byte_counter()
    try:
        assert commit.checkin_blob_sha(root, "missing.txt") is None
        assert commit.bytes_read_total() == 0
    finally:
        commit.reset_byte_counter(token)


def test_nested_bind_shares_the_outer_box(tmp_path):
    outer = commit.bind_byte_counter()
    try:
        assert commit.bind_byte_counter() is None
    finally:
        commit.reset_byte_counter(outer)
