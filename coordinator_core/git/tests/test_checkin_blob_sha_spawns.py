"""checkin_blob_sha adds the bytes it reads to the bound byte counter and nothing else changes."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from coordinator_core.git import commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def test_bounded_read_still_counts_bytes_on_the_worker(tmp_path):
    root = _repo(tmp_path)
    (root / "a.txt").write_bytes(b"a" * 7)
    token = commit.bind_byte_counter()
    try:
        assert commit.checkin_blob_sha(root, "a.txt") is not None
        assert commit.bytes_read_total() == 7
    finally:
        commit.reset_byte_counter(token)


@pytest.mark.deliberate_wall_clock(reason="bounds the caller's wait on a stat blocked for seconds")
def test_stalled_read_answers_none_within_the_caller_bound(tmp_path, monkeypatch):
    import os
    import threading

    root = _repo(tmp_path)
    (root / "slow.txt").write_bytes(b"x\n")
    release = threading.Event()
    real_stat = os.stat

    def _blocked(path, *a, **k):
        if str(path).endswith("slow.txt"):
            release.wait(10)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", _blocked)
    try:
        assert commit.checkin_blob_sha(root, "slow.txt") is None
        assert not release.is_set()  # returned while the stat was still blocked
    finally:
        release.set()
