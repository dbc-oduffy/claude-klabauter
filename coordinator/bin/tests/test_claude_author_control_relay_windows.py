"""Tests for claude_author_control/relay_windows.py (ConPTY relay): pure parts everywhere, ConPTY on win32."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from claude_author_control import relay_windows as rw  # noqa: E402

win32 = pytest.mark.skipif(sys.platform != "win32", reason="ConPTY is Windows-only")


def test_import_safe_without_windll():
    assert not hasattr(rw, "windll")
    if sys.platform != "win32":
        with pytest.raises(OSError):
            rw.spawn(["x"], {}, object())


def test_env_block_sorted_and_double_nul_terminated():
    block = rw.build_env_block({"b": "2", "A": "1", "Path": "p"})
    assert block == "A=1\0b=2\0Path=p\0\0"
    assert rw.build_env_block({}) == "\0"


def test_env_block_excludes_absent_variable():
    assert "CLAUDE_CODE_CHILD_SESSION" not in rw.build_env_block({"X": "1"})


def test_create_process_contract():
    assert rw.INHERIT_HANDLES is False
    assert rw.STARTF_FLAGS == rw.STARTF_USESTDHANDLES  # NULL std handles: child binds to the pty
    assert rw.CREATION_FLAGS == rw.EXTENDED_STARTUPINFO_PRESENT | rw.CREATE_UNICODE_ENVIRONMENT
    for placement in (0x08000000, 0x00000008, 0x00000010):  # NO_WINDOW, DETACHED, NEW_CONSOLE
        assert not rw.CREATION_FLAGS & placement


def test_cmdline_quoting():
    assert rw.build_cmdline(["python", "-c", "print('a b')"]) == 'python -c "print(\'a b\')"'


class _Obs:
    def __init__(self):
        self.out = bytearray()
        self.inp = bytearray()

    def on_output(self, data):
        self.out += data

    def on_input(self, data):
        self.inp += data


def _run(code: str, env=None, feed: bytes = b"", settle: float = 1.0):
    obs = _Obs()
    env = dict(os.environ) if env is None else env
    relay = rw.ConPtyRelay(rw._get_api(), [sys.executable, "-c", code], env, obs,
                           attach_console=False)
    if feed:
        time.sleep(settle)
        relay.write(feed)
    return relay, obs


@win32
def test_conpty_relays_output_and_exit_code():
    relay, obs = _run("import sys; print('hello-conpty'); sys.exit(7)")
    assert relay.wait() == 7
    assert b"hello-conpty" in bytes(obs.out)
    assert relay.alive() is False


@win32
def test_conpty_write_reaches_child_and_esc_is_raw():
    # A cooked console line-edits input (ESC clears the line), so the child enters raw VT input
    # mode the way a TUI does; the assertion is that the relay delivers the byte unchanged.
    code = ("import ctypes,os,sys; k=ctypes.WinDLL('kernel32'); "
            "k.GetStdHandle.restype=ctypes.c_void_p; "
            "k.SetConsoleMode(ctypes.c_void_p(k.GetStdHandle(-10)), 0x200); "
            "d=os.read(0,8); print('GOT', d.hex()); sys.stdout.flush()")
    relay, obs = _run(code, feed=b"\x1b")
    assert relay.wait() == 0
    assert b"GOT 1b" in bytes(obs.out)


@win32
def test_conpty_env_is_exactly_passed_env():
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_CHILD_SESSION"}
    os.environ["CLAUDE_CODE_CHILD_SESSION"] = "1"
    try:
        relay, obs = _run("import os; print('V=' + str(os.environ.get('CLAUDE_CODE_CHILD_SESSION')))",
                          env=env)
        assert relay.wait() == 0
    finally:
        del os.environ["CLAUDE_CODE_CHILD_SESSION"]
    assert b"V=None" in bytes(obs.out)


@win32
def test_conpty_child_not_in_supervisor_console_process_list():
    import ctypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    procs = (ctypes.c_uint * 64)()
    if k.GetConsoleProcessList(procs, 64) == 0:
        k.AllocConsole()
    relay, _ = _run("import time; time.sleep(30)")
    try:
        pid = ctypes.c_uint(0)
        k.GetProcessId.restype = ctypes.c_uint
        child = k.GetProcessId(relay._h_proc)
        n = k.GetConsoleProcessList(procs, 64)
        listed = set(procs[:n])
        assert os.getpid() in listed
        assert child not in listed, "ConPTY child attached to the invoking console"
    finally:
        k.TerminateProcess(relay._h_proc, 0)
        relay.wait()
