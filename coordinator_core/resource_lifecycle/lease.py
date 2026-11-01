"""Lease held by an OS-owned endpoint: a Windows first-instance named pipe or a POSIX AF_UNIX socket.

The kernel releases the endpoint when the holder dies. No PID is read or written, and no file's
existence or content decides who holds the lease; the POSIX ``<socket>.lock`` file is only an
``flock`` target that serializes the probe/unlink/bind sequence among contenders.
"""

from __future__ import annotations

import errno
import os
import re
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.resource_lifecycle.errors import LeaseHeld

__all__ = ["Lease"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PIPE_PREFIX = "\\\\.\\pipe\\coordinator-lease."
_ERROR_ACCESS_DENIED = 5
_ERROR_PIPE_BUSY = 231
_SUN_PATH_MAX_BYTES = 100
_PROBE_TIMEOUT_SECS = 0.25
_BACKLOG = 1


class Lease:
    """Exclusive named lease. ``acquire`` raises ``LeaseHeld`` while another live owner holds it.

    POSIX needs ``directory`` (where the socket is bound); Windows ignores it.
    """

    def __init__(self, name: str, directory: Optional[os.PathLike] = None) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(f"lease name must match {_NAME_RE.pattern}: {name!r}")
        self.name = name
        self._directory = Path(directory) if directory is not None else None
        self._handle: Optional[int] = None
        self._sock = None
        self._sock_path: Optional[Path] = None
        self._sock_identity: Optional[tuple] = None

    @property
    def held(self) -> bool:
        return self._handle is not None or self._sock is not None

    def acquire(self) -> "Lease":
        if self.held:
            raise RuntimeError(f"lease {self.name!r} already held by this object")
        if sys.platform == "win32":
            self._acquire_pipe()
        else:
            self._acquire_socket()
        return self

    def release(self) -> None:
        if self._handle is not None:
            import _winapi

            handle, self._handle = self._handle, None
            try:
                _winapi.CloseHandle(handle)
            except OSError:
                pass
        if self._sock is not None:
            sock, self._sock = self._sock, None
            try:
                sock.close()
            except OSError:
                pass
            self._unlink_if_owned()

    def __enter__(self) -> "Lease":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()

    def _acquire_pipe(self) -> None:
        import _winapi

        try:
            self._handle = _winapi.CreateNamedPipe(
                _PIPE_PREFIX + self.name,
                _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
                _winapi.PIPE_WAIT,
                _winapi.PIPE_UNLIMITED_INSTANCES,
                512,
                512,
                0,
                0,
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) in (_ERROR_ACCESS_DENIED, _ERROR_PIPE_BUSY):
                raise LeaseHeld(f"lease {self.name!r} is held by another owner") from exc
            raise

    def _acquire_socket(self) -> None:
        import fcntl
        import socket

        if self._directory is None:
            raise ValueError("a POSIX lease needs a directory to bind its socket in")
        os.makedirs(self._directory, mode=0o700, exist_ok=True)
        path = self._directory / f"{self.name}.sock"
        if len(os.fsencode(str(path))) > _SUN_PATH_MAX_BYTES:
            raise ValueError(f"socket path over the {_SUN_PATH_MAX_BYTES}-byte sun_path budget: {str(path)!r}")

        lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise LeaseHeld(f"lease {self.name!r} is held by another owner") from exc
                raise
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            identity = None
            try:
                try:
                    sock.bind(str(path))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    if not _probe_is_stale(path):
                        raise LeaseHeld(f"lease {self.name!r} is held by another owner") from exc
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                    try:
                        sock.bind(str(path))
                    except OSError as exc2:
                        if exc2.errno == errno.EADDRINUSE:
                            raise LeaseHeld(f"lease {self.name!r} is held by another owner") from exc2
                        raise
                st = os.lstat(path)
                identity = (st.st_dev, st.st_ino)
                os.chmod(path, 0o600)
                sock.listen(_BACKLOG)
            except BaseException:
                sock.close()
                if identity is not None:
                    self._sock_path, self._sock_identity = path, identity
                    self._unlink_if_owned()
                raise
            self._sock, self._sock_path, self._sock_identity = sock, path, identity
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

    def _unlink_if_owned(self) -> None:
        path, identity = self._sock_path, self._sock_identity
        self._sock_path = self._sock_identity = None
        if path is None or identity is None:
            return
        try:
            st = os.lstat(path)
            if (st.st_dev, st.st_ino) == identity:
                os.unlink(path)
        except OSError:
            pass


def _probe_is_stale(path: Path) -> bool:
    """True only when connect is refused: the path exists with no listener behind it."""
    import socket

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_PROBE_TIMEOUT_SECS)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        return True
    except FileNotFoundError:
        return False
    except (TimeoutError, socket.timeout):
        return False
    finally:
        probe.close()
    return False
