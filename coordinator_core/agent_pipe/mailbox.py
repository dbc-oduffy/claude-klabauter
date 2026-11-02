"""agent_pipe.mailbox -- append-only per-session mailbox that workflow_watch.mailbox_line wakes on.

append is one os.write on an O_APPEND fd so concurrent relays never interleave a line. The
mailbox is a file the watcher tails; drain renames it away, reads it, and deletes it so a later append starts a fresh file.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

from coordinator_core.agent_pipe.contract import PipeMessage, mailbox_path

_CONSUMED_SUFFIX = ".consumed"


@contextlib.contextmanager
def _append_lock(path: Path):
    """Windows only: the CRT emulates O_APPEND as seek-then-write, so serialize on a sibling .lock."""
    if sys.platform != "win32":
        yield
        return
    import msvcrt

    fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                break
            except OSError:
                time.sleep(0.01)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def append(msg: PipeMessage) -> Path:
    """Append msg as one newline-terminated line to its session's mailbox; returns the mailbox path."""
    path = mailbox_path(msg.to_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (msg.to_json_line() + "\n").encode("utf-8")
    with _append_lock(path):
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    return path


def drain(session_id: str) -> list[str]:
    """Take every line currently in the session's mailbox; the file is renamed to a consumed name, read, then deleted.

    Returns [] when there is no mailbox. A later append creates a fresh file.
    Trap: an appender that opened the path before the rename and writes after the read loses that line.
    """
    path = mailbox_path(session_id)
    consumed = path.with_name(f"{path.name}{_CONSUMED_SUFFIX}.{os.getpid()}.{os.urandom(4).hex()}")
    try:
        os.replace(path, consumed)
    except FileNotFoundError:
        return []
    try:
        return consumed.read_text(encoding="utf-8").splitlines()
    finally:
        consumed.unlink(missing_ok=True)
