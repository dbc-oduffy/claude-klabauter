"""Write stall diagnostics off the calling thread so a dispatch-gate thread never waits on the disk."""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from typing import Callable, Optional

QUEUE_MAX = 64

_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_MAX)
_start_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_made_dirs: set = set()
_dropped = 0
_pending = 0
_idle = threading.Condition()


def _mkdir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_once_dir(path: Path, text: str) -> None:
    directory = path.parent
    if directory not in _made_dirs:
        _mkdir(directory)
        _made_dirs.add(directory)
    try:
        _write(path, text)
    except FileNotFoundError:
        _made_dirs.discard(directory)
        _mkdir(directory)
        _made_dirs.add(directory)
        _write(path, text)


def _run() -> None:
    global _pending
    while True:
        path, text, claim, stub_text = _queue.get()
        try:
            chosen: Optional[str] = text
            if claim is not None:
                try:
                    won = bool(claim())
                except Exception:  # noqa: BLE001 -- an unusable claim keeps the diagnostic
                    won = True
                if not won:
                    chosen = stub_text
            if chosen is not None:
                _write_once_dir(Path(path), chosen)
        except Exception:  # noqa: BLE001 -- diagnostics never stop the writer
            pass
        finally:
            with _idle:
                _pending -= 1
                _idle.notify_all()


def _ensure_thread() -> None:
    global _thread
    if _thread is not None:
        return
    with _start_lock:
        if _thread is None:
            t = threading.Thread(target=_run, name="stall-writer", daemon=True)
            t.start()
            _thread = t


def submit(
    path: object,
    text: str,
    *,
    claim: Optional[Callable[[], bool]] = None,
    stub_text: Optional[str] = None,
) -> bool:
    """Queue `text` for writing to `path`; False when the bounded queue is full.

    `claim`, when given, runs on the writer thread (the episode claim touches the disk): a False
    verdict writes `stub_text` instead, or nothing when `stub_text` is None. Never blocks and
    raises nothing; a full queue is counted in `dropped_count`.
    """
    global _dropped, _pending
    try:
        _ensure_thread()
        with _idle:
            _pending += 1
        try:
            _queue.put_nowait((path, text, claim, stub_text))
        except queue.Full:
            with _idle:
                _pending -= 1
                _dropped += 1
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def dropped_count() -> int:
    """Number of submissions refused because the queue was full."""
    return _dropped


def flush(timeout: float = 5.0) -> bool:
    """Wait until every queued write has finished; False on timeout. Test and shutdown aid."""
    with _idle:
        return _idle.wait_for(lambda: _pending == 0, timeout=timeout)
