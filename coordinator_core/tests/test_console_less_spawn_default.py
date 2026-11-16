"""install_console_less_spawn_default: console-less parents spawn windowless children.

Regression: engine pool workers run under pythonw (no console); every git/console
child they spawned allocated a visible, focus-stealing console (Machine-a, 2026-11-15).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import win_portability as wp

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows console semantics")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch):
    import _winapi

    calls = []
    monkeypatch.setattr(_winapi, "CreateProcess", lambda *a: calls.append(a[5]))
    return calls


def _spawn(flags) -> None:
    import _winapi

    _winapi.CreateProcess(None, "x", None, None, False, flags, None, None, None)


@windows_only
@pytest.mark.parametrize(
    "no_console, flags, expected",
    [
        (True, 0, wp._CREATE_NO_WINDOW),
        (True, wp._DETACHED_PROCESS, wp._DETACHED_PROCESS),
        (True, wp._CREATE_NEW_CONSOLE, wp._CREATE_NEW_CONSOLE),
        (False, 0, 0),
    ],
)
def test_flag_added_only_when_console_less_and_unchosen(recorded, monkeypatch: pytest.MonkeyPatch, no_console, flags, expected) -> None:
    monkeypatch.setattr(wp, "_process_has_no_console", lambda: no_console)
    wp.install_console_less_spawn_default()
    _spawn(flags)
    assert recorded == [expected]


@windows_only
def test_install_is_idempotent(recorded, monkeypatch: pytest.MonkeyPatch) -> None:
    import _winapi

    monkeypatch.setattr(wp, "_process_has_no_console", lambda: True)
    wp.install_console_less_spawn_default()
    first = _winapi.CreateProcess
    wp.install_console_less_spawn_default()
    assert _winapi.CreateProcess is first


@windows_only
def test_pythonw_parent_child_gets_windowless_console(tmp_path: Path) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("no pythonw.exe beside the interpreter")
    out = tmp_path / "child.txt"
    child = (
        "import ctypes,sys;k=ctypes.windll.kernel32;"
        f"open(r'{out}','w').write(f'{{k.GetConsoleCP()}} {{k.GetConsoleWindow()}}')"
    )
    parent = (
        "import subprocess,sys;"
        f"sys.path.insert(0, r'{Path(wp.__file__).parents[1]}');"
        "from coordinator_core.win_portability import install_console_less_spawn_default as i;i();"
        f"subprocess.run([r'{sys.executable}', '-I', '-c', {child!r}], stdin=subprocess.DEVNULL, timeout=30)"
    )
    subprocess.run([str(pythonw), "-I", "-S", "-c", parent], timeout=60, check=True)
    cp, hwnd = out.read_text().split()
    assert cp != "0", "child must have a console, so its own subtree inherits it"
    assert hwnd == "0", "that console must have no window"
