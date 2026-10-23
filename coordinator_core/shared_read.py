"""Read a whole file without blocking a concurrent replace of that file.

CPython ``open()`` on Windows never requests ``FILE_SHARE_DELETE``, so a reader
holding its handle (for example through a ``tomllib.load(f)`` parse) makes every
rename onto that path fail with a sharing violation or WinError 5. Measured on
this box: 3 polling readers x 300 replaces failed 224-273 times with plain
readers and 0 times with ``FILE_SHARE_DELETE`` readers plus a POSIX-semantics
rename.

Invariant: the file is read fully and the handle closed before this function
returns, so the caller parses bytes, never a live handle.

Stdlib only. The Windows path uses ``_winapi`` (no ``ctypes`` import) so the
read stays cheap on every guard's registry resolve.
"""

from __future__ import annotations

import os
import sys

__all__ = ["read_bytes_shared"]

_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | WRITE | DELETE
_OPEN_EXISTING = 3


def _open_fd_windows(path: str) -> int:
    """Return a read-only CRT fd on ``path`` opened with FILE_SHARE_DELETE.

    Missing file or directory surfaces as ``FileNotFoundError`` (CPython maps
    winerror 2/3 on ``OSError`` construction); other failures keep ``winerror``.
    """
    import msvcrt

    try:
        import _winapi
    except ImportError:  # pragma: no cover - non-CPython
        handle = _create_file_ctypes(path)
    else:
        handle = _winapi.CreateFile(
            path, _GENERIC_READ, _SHARE_ALL, 0, _OPEN_EXISTING, 0, 0
        )
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
    except BaseException:
        _close_handle(int(handle))
        raise


def _create_file_ctypes(path: str) -> int:
    import ctypes
    from ctypes import wintypes

    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(path, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, 0, None)
    if handle in (None, 0, -1, 0xFFFFFFFF, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close_handle(handle: int) -> None:
    try:
        import _winapi

        _winapi.CloseHandle(handle)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def read_bytes_shared(path: "str | os.PathLike[str]") -> bytes:
    """Return the full bytes of ``path``; the handle is closed before return.

    Raises ``FileNotFoundError`` for a missing file exactly like ``open()``.
    """
    p = os.fspath(path)
    if sys.platform != "win32":
        with open(p, "rb") as f:
            return f.read()
    fd = _open_fd_windows(p)
    with os.fdopen(fd, "rb") as f:
        return f.read()
