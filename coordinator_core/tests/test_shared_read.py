"""Tests for coordinator_core.shared_read.read_bytes_shared."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.shared_read import read_bytes_shared

_WIN = sys.platform == "win32"


def test_returns_exact_bytes(tmp_path):
    p = tmp_path / "f.bin"
    data = b"a\r\nb\x00\xff" * 1000
    p.write_bytes(data)
    assert read_bytes_shared(p) == data
    assert read_bytes_shared(str(p)) == data


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_bytes_shared(tmp_path / "nope.toml")
    with pytest.raises(FileNotFoundError):
        read_bytes_shared(tmp_path / "nodir" / "nope.toml")


def test_posix_path_reads_bytes(tmp_path):
    if _WIN:
        pytest.skip("POSIX-only branch")
    p = tmp_path / "f"
    p.write_bytes(b"x")
    assert read_bytes_shared(p) == b"x"


def _posix_rename(src: str, dst: str) -> None:
    """Rename ``src`` onto ``dst`` with FILE_RENAME_FLAG_POSIX_SEMANTICS."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    k32.SetFileInformationByHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    DELETE = 0x00010000
    h = k32.CreateFileW(src, DELETE, 0x7, None, 3, 0x80, None)
    assert h not in (None, -1, 0xFFFFFFFF, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    try:
        name = os.path.abspath(dst)
        name_bytes = name.encode("utf-16-le")
        ptr = ctypes.sizeof(ctypes.c_void_p)
        header =((4 + ptr - 1) // ptr) * ptr + ptr + 4
        buf = ctypes.create_string_buffer(header + len(name_bytes) + 2)
        root_off = ((4 + ptr - 1) // ptr) * ptr
        ctypes.c_uint32.from_buffer(buf, 0).value = 0x1 | 0x2
        ctypes.c_void_p.from_buffer(buf, root_off).value = None
        ctypes.c_uint32.from_buffer(buf, root_off + ptr).value = len(name_bytes)
        buf[header : header + len(name_bytes)] = name_bytes
        ok = k32.SetFileInformationByHandle(h, 22, buf, len(buf))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


@pytest.mark.skipif(not _WIN, reason="Windows share-mode semantics")
def test_open_shared_handle_does_not_block_posix_rename(tmp_path):
    import _winapi
    import msvcrt

    target = tmp_path / "registry.toml"
    target.write_bytes(b"old")
    other = tmp_path / "registry.toml.tmp"
    other.write_bytes(b"new")

    handle = _winapi.CreateFile(str(target), 0x80000000, 0x7, 0, 3, 0, 0)
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    try:
        _posix_rename(str(other), str(target))
    finally:
        os.close(fd)
    assert target.read_bytes() == b"new"


@pytest.mark.skipif(not _WIN, reason="Windows share-mode semantics")
def test_plain_open_holder_blocks_posix_rename(tmp_path):
    target = tmp_path / "registry.toml"
    target.write_bytes(b"old")
    other = tmp_path / "registry.toml.tmp"
    other.write_bytes(b"new")
    with open(target, "rb"):
        with pytest.raises(OSError):
            _posix_rename(str(other), str(target))
    assert target.read_bytes() == b"old"


@pytest.mark.spawns_process
@pytest.mark.skipif(not _WIN, reason="Windows import path")
def test_winapi_path_imports_no_ctypes(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x")
    code = (
        "import sys; from coordinator_core.shared_read import read_bytes_shared;"
        "m = 'ctypes' in sys.modules;"
        f"read_bytes_shared({str(p)!r});"
        "sys.exit(3 if ('ctypes' in sys.modules and not m) else 0)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert r.returncode == 0, r.stderr
