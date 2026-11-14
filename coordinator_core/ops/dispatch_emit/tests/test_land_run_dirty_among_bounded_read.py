"""land_run's dirty scan settles a worktree path whose bytes cannot be read promptly as changed.

Blocking file class: a non-regular file (a named pipe or device node reached through a tracked or
declared path). ``open`` on a FIFO with no writer waits for one, so ``Path.read_bytes`` parks the
warm op on I/O with near-zero CPU. A regular file whose bytes do
not match its stat size (a file mid-write, or a placeholder whose size lies) is the same class: it
is settled as ``_FILTERED`` from the stat alone, never from a blocking read.
"""

from __future__ import annotations

import os
import stat as stat_mod
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "base.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "base"], root)
    return root


def _fake_stat(monkeypatch, target: Path, *, mode=None, size=None) -> None:
    real = os.stat
    want = os.path.normcase(str(target))

    def stat(path, *a, **kw):
        st = real(path, *a, **kw)
        if os.path.normcase(str(path)) != want:
            return st
        fields = list(st)
        if mode is not None:
            fields[0] = mode
        if size is not None:
            fields[6] = size
        return os.stat_result(fields)

    monkeypatch.setattr(os, "stat", stat)


def test_non_regular_stat_is_filtered_without_reading_bytes(repo, monkeypatch):
    (repo / "dev.txt").write_bytes(b"would-be-read\n")
    _fake_stat(monkeypatch, repo / "dev.txt", mode=stat_mod.S_IFCHR | 0o600)
    token = commit.bind_byte_counter()
    try:
        changed, _ignored, _head = land_run._dirty_among(repo, ["dev.txt"])
        read = commit.bytes_read_total()
    finally:
        commit.reset_byte_counter(token)
    assert "dev.txt" in changed
    assert read == 0
    assert land_run._hashed_blob(repo, "dev.txt") is land_run._FILTERED


def test_size_mismatching_bytes_are_filtered(repo, monkeypatch):
    (repo / "grow.txt").write_bytes(b"abc\n")
    _fake_stat(monkeypatch, repo / "grow.txt", size=2)
    assert commit.checkin_blob_sha(repo, "grow.txt") is None
    changed, _i, _h = land_run._dirty_among(repo, ["grow.txt"])
    assert "grow.txt" in changed


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX named pipe")
def test_fifo_without_writer_returns_promptly(repo):
    os.mkfifo(repo / "pipe.txt")
    t0 = time.monotonic()
    changed, _i, _h = land_run._dirty_among(repo, ["pipe.txt"])
    assert time.monotonic() - t0 < 1.0
    assert "pipe.txt" in changed


def test_regular_file_keeps_checkin_form_hash(repo):
    (repo / "plain.txt").write_bytes(b"hello\n")
    import hashlib

    assert commit.checkin_blob_sha(repo, "plain.txt") == hashlib.sha1(b"blob 6\0hello\n").hexdigest()
    assert commit.checkin_blob_sha(repo, "base.txt") == _git(["rev-parse", "HEAD:base.txt"], repo).strip()


def test_200_path_scan_spawns_nothing_and_stays_under_200ms(repo, monkeypatch):
    paths = []
    for i in range(200):
        rel = f"d/f{i}.txt"
        (repo / "d").mkdir(exist_ok=True)
        (repo / rel).write_bytes(b"x%d\n" % i)
        paths.append(rel)
    spawns: list = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **kw):
        spawns.append(a)
        return real_popen(*a, **kw)

    land_run._dirty_among(repo, paths[:1])  # warm imports
    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    c0 = time.process_time()
    changed, _i, _h = land_run._dirty_among(repo, paths)
    cpu = time.process_time() - c0
    assert spawns == []
    assert changed == set(paths)
    assert cpu < 0.2
