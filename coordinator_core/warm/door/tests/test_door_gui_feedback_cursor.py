"""door.c ends the AppStarting cursor: GUI variant pumps at main, every child launch forces feedback off.

Pins door.c's three `STARTUPINFOW si;` sites, the DOOR_WINDOWLESS-guarded
PeekMessageW at the top of main, the windowless-only build flags, and (on
Windows) that the committed door-w.exe reaches input idle.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_DOOR_DIR = Path(__file__).resolve().parent.parent
_DOOR_C = _DOOR_DIR / "door.c"


def _load_build():
    import importlib.util

    spec = importlib.util.spec_from_file_location("door_build_under_test", _DOOR_DIR / "build.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _si_sites(text: str) -> list[str]:
    """Each `STARTUPINFOW si;` declaration through its CreateProcessW call."""
    sites = []
    for m in re.finditer(r"STARTUPINFOW si;", text):
        end = text.index("CreateProcessW(", m.end())
        sites.append(text[m.start():end])
    return sites


def test_every_startupinfo_site_forces_feedback_off():
    text = _DOOR_C.read_text(encoding="utf-8")
    sites = _si_sites(text)
    assert len(sites) == 3
    for site in sites:
        assert re.search(r"si\.dwFlags\s*=[^;]*STARTF_FORCEOFFFEEDBACK", site), site


def test_feedback_flag_is_defined_for_old_sdks():
    text = _DOOR_C.read_text(encoding="utf-8")
    assert re.search(
        r"#ifndef STARTF_FORCEOFFFEEDBACK\s+#define STARTF_FORCEOFFFEEDBACK 0x00000080", text
    )


def test_peek_message_is_first_and_windowless_guarded():
    text = _DOOR_C.read_text(encoding="utf-8")
    main = text[text.index("int main(void) {"):]
    head = main[: main.index("#if DOOR_PRIVATE_REQUIRED")]
    assert "#ifdef DOOR_WINDOWLESS" in head
    assert "PeekMessageW(&" in head and "PM_NOREMOVE" in head
    assert head.index("#ifdef DOOR_WINDOWLESS") < head.index("PeekMessageW(")
    assert head.index("PeekMessageW(") < head.index("#endif")
    assert text.count("PeekMessageW(") == 1


@pytest.mark.parametrize("kind", ["clang", "cl"])
def test_windowless_flags_carry_user32_and_define_only_there(kind):
    build = _load_build()
    gui = build._compile_flags(kind, True)
    console = build._compile_flags(kind, False)
    define, lib = (
        ("-DDOOR_WINDOWLESS", "-luser32") if kind == "clang" else ("/DDOOR_WINDOWLESS", "User32.lib")
    )
    assert define in gui and lib in gui
    assert define not in console and lib not in console


@pytest.mark.skipif(sys.platform != "win32", reason="WaitForInputIdle is Windows-only")
def test_committed_gui_door_reaches_input_idle():
    import ctypes
    from ctypes import wintypes

    exe = _DOOR_DIR / "door-w.exe"
    proc = subprocess.Popen(
        [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.WaitForInputIdle.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        user32.WaitForInputIdle.restype = wintypes.DWORD
        assert user32.WaitForInputIdle(int(proc._handle), 1000) == 0
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
