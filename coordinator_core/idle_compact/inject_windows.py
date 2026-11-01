"""Windows console injector: types ``/compact`` into the console owned by a claude.exe pid.

``FreeConsole`` detaches the caller from its own console, so ``WindowsInjector.inject``
never runs the sequence in-process: it launches ``python -m
coordinator_core.idle_compact.inject_windows <pid>`` as a DETACHED_PROCESS child and
reads the outcome from the child's exit code. The Enter key goes in a separate
write after a pause; in the same burst the TUI reads it as a paste and does not send.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any, Callable, Optional

from coordinator_core.idle_compact.contract import InjectResult

KEY_EVENT = 0x0001
VK_RETURN = 0x0D
COMMAND_TEXT = "/compact"
ENTER_DELAY_SECONDS = 0.5
CHILD_TIMEOUT_SECONDS = 10.0

GENERIC_READ_WRITE = 0xC0000000
SHARE_READ_WRITE = 3
OPEN_EXISTING = 3

EXIT_OK = 0
EXIT_ATTACH_FAILED = 2
EXIT_OPEN_FAILED = 3
EXIT_WRITE_FAILED = 4
EXIT_USAGE = 64

_EXIT_REASONS = {
    EXIT_ATTACH_FAILED: "AttachConsole failed",
    EXIT_OPEN_FAILED: "CreateFileW(CONIN$) failed",
    EXIT_WRITE_FAILED: "WriteConsoleInputW failed",
    EXIT_USAGE: "bad arguments",
}

_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


def records_for(text: str) -> list:
    """One key-down and one key-up INPUT_RECORD per character of ``text``."""
    recs = []
    for ch in text:
        vk = VK_RETURN if ch == "\r" else 0
        for down in (True, False):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            r.Event.KeyEvent.bKeyDown = down
            r.Event.KeyEvent.wRepeatCount = 1
            r.Event.KeyEvent.wVirtualKeyCode = vk
            r.Event.KeyEvent.uChar = ch
            recs.append(r)
    return recs


def _write_records(k32: Any, handle: Any, text: str) -> bool:
    recs = records_for(text)
    arr = (INPUT_RECORD * len(recs))(*recs)
    written = wintypes.DWORD()
    return bool(k32.WriteConsoleInputW(handle, arr, len(recs), ctypes.byref(written)))


def run_sequence(
    pid: int,
    k32: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """FreeConsole, AttachConsole(pid), open CONIN$, type the text, pause, send Enter.

    Returns an ``EXIT_*`` code. Must run in a process that owns no console it needs.
    """
    if k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return EXIT_ATTACH_FAILED
    try:
        handle = k32.CreateFileW(
            "CONIN$", GENERIC_READ_WRITE, SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None
        )
        if handle in (None, 0, _INVALID_HANDLE, -1):
            return EXIT_OPEN_FAILED
        if not _write_records(k32, handle, COMMAND_TEXT):
            return EXIT_WRITE_FAILED
        sleep(ENTER_DELAY_SECONDS)
        if not _write_records(k32, handle, "\r"):
            return EXIT_WRITE_FAILED
        return EXIT_OK
    finally:
        k32.FreeConsole()


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        pid = int(args[0])
    except (IndexError, ValueError):
        return EXIT_USAGE
    return run_sequence(pid)


class WindowsInjector:
    """``Injector`` for Windows: spawns the detached child and maps its exit code."""

    def __init__(self, timeout: float = CHILD_TIMEOUT_SECONDS, popen: Any = subprocess.Popen):
        self._timeout = timeout
        self._popen = popen

    def inject(self, pid: int) -> InjectResult:
        if sys.platform != "win32":
            return InjectResult(False, "not windows")
        try:
            child = self._popen(
                [sys.executable, "-m", "coordinator_core.idle_compact.inject_windows", str(int(pid))],
                creationflags=_DETACHED_PROCESS | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),  # popup-intentional-last-resort
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            code = child.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            return InjectResult(False, "injector child timed out")
        except OSError as exc:
            return InjectResult(False, f"injector spawn failed: {exc}")
        if code == EXIT_OK:
            return InjectResult(True, "injected")
        return InjectResult(False, _EXIT_REASONS.get(code, f"injector child exit {code}"))


if __name__ == "__main__":
    sys.exit(main())
