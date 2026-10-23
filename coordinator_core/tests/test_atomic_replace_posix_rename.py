"""atomic_write_bytes lands against FILE_SHARE_DELETE readers without sleeping."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from coordinator_core import atomic_replace

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows rename semantics")

# Bound before any test patches ``time.sleep`` (a module-global): the holder
# thread must keep its real sleep while the retry ladder's sleep is trapped.
_REAL_SLEEP = time.sleep


def _hold_shared(target, hold_s: float, seen: list):
    import _winapi
    import msvcrt

    opened = threading.Event()

    def run() -> None:
        h = _winapi.CreateFile(
            str(target), 0x80000000, 0x7, 0, 3, 0, 0
        )
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY)
        try:
            opened.set()
            _REAL_SLEEP(hold_s)
            seen.append(os.read(fd, 100))
        finally:
            os.close(fd)

    t = threading.Thread(target=run)
    t.start()
    assert opened.wait(5)
    return t


@win_only
def test_share_delete_reader_does_not_block_first_try(tmp_path, monkeypatch):
    target = tmp_path / "f.bin"
    target.write_bytes(b"old")
    seen: list = []
    t = _hold_shared(target, 0.5, seen)

    def no_sleep(_s):
        raise AssertionError("retry ladder ran")

    monkeypatch.setattr(atomic_replace.time, "sleep", no_sleep)
    atomic_replace.atomic_write_bytes(target, b"new")
    t.join()
    assert target.read_bytes() == b"new"
    assert seen == [b"old"]


@win_only
def test_flag_rejection_falls_back_to_os_replace(tmp_path, monkeypatch):
    target = tmp_path / "f.bin"
    target.write_bytes(b"old")
    monkeypatch.setattr(atomic_replace, "_posix_rename_windows", lambda s, d: False)
    calls = []
    real = os.replace
    monkeypatch.setattr(
        atomic_replace.os, "replace", lambda s, d: (calls.append((s, d)), real(s, d))
    )
    atomic_replace.atomic_write_bytes(target, b"new")
    assert len(calls) == 1
    assert target.read_bytes() == b"new"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path")
def test_posix_uses_plain_os_replace(tmp_path, monkeypatch):
    def boom(*a):
        raise AssertionError("windows rename called on POSIX")

    monkeypatch.setattr(atomic_replace, "_posix_rename_windows", boom)
    target = tmp_path / "f.bin"
    target.write_bytes(b"old")
    atomic_replace.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
