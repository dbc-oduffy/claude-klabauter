"""POSIX injector: types ``/compact`` into the claude process's tty with TIOCSTI.

Pure Python, no shell-out. Where the kernel refuses TIOCSTI (Linux >= 6.2
``legacy_tiocsti=0``, hardened macOS) the result is ``InjectResult(False,
"injector-unavailable")`` and nothing is typed. Enter is sent as a separate
write after a pause so the burst is not read as a paste.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from typing import Optional

from coordinator_core.idle_compact.contract import InjectResult

try:
    import fcntl
except ImportError:  # Windows: the module imports, inject() reports unavailable.
    fcntl = None  # type: ignore[assignment]

try:
    import termios
except ImportError:
    termios = None  # type: ignore[assignment]

COMMAND = "/compact"
ENTER_DELAY_SECONDS = 0.5
UNAVAILABLE = "injector-unavailable"

_TIOCSTI_LINUX = 0x5412
_TIOCSTI_DARWIN = 0x80017472

_REFUSED_ERRNOS = frozenset({errno.EPERM, errno.EIO, errno.EACCES, errno.ENOTTY})


def _tiocsti() -> int:
    value = getattr(termios, "TIOCSTI", None)
    if value is not None:
        return value
    return _TIOCSTI_DARWIN if sys.platform == "darwin" else _TIOCSTI_LINUX


def resolve_tty(pid: int) -> Optional[str]:
    """Tty device path of ``pid``: /proc fd 0 on Linux, psutil ``terminal()`` elsewhere; None if unknown."""
    if sys.platform.startswith("linux"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/0")
        except OSError:
            return None
        return target if target.startswith("/dev/") else None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.Process(pid).terminal() or None
    except Exception:
        return None


class PosixInjector:
    """``Injector`` for POSIX hosts; one ``inject(pid)`` types ``/compact`` then Enter."""

    def __init__(self, sleep=time.sleep) -> None:
        self._sleep = sleep

    def inject(self, pid: int) -> InjectResult:
        if fcntl is None:
            return InjectResult(False, UNAVAILABLE)
        tty = resolve_tty(pid)
        if tty is None:
            return InjectResult(False, "tty-unresolved")
        request = _tiocsti()
        fd = -1
        try:
            fd = os.open(tty, os.O_RDWR | getattr(os, "O_NOCTTY", 0))
            for ch in COMMAND:
                fcntl.ioctl(fd, request, ch.encode())
            self._sleep(ENTER_DELAY_SECONDS)
            fcntl.ioctl(fd, request, b"\r")
        except OSError as exc:
            if exc.errno in _REFUSED_ERRNOS:
                return InjectResult(False, UNAVAILABLE)
            return InjectResult(False, f"inject-failed: errno {exc.errno}")
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return InjectResult(True, "injected")
