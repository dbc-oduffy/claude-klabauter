"""unimatrix_python: pin, else darwin Homebrew candidate, else console python."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "python_interp_under_test", Path(__file__).resolve().parents[1] / "lib" / "python_interp.py")
pi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pi)

BREW = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")


def test_pin_wins_even_on_darwin_with_homebrew():
    assert pi.unimatrix_python("/pin/py", platform="darwin", is_executable=lambda p: True) == "/pin/py"


def test_darwin_takes_first_executable_homebrew():
    ok = {BREW[1]}
    assert pi.unimatrix_python(None, platform="darwin", homebrew=BREW, is_executable=ok.__contains__) == BREW[1]
    assert pi.unimatrix_python("", platform="darwin", homebrew=BREW, is_executable=lambda p: True) == BREW[0]


def test_darwin_without_homebrew_falls_to_console():
    assert pi.unimatrix_python(None, platform="darwin", is_executable=lambda p: False) == pi.resolve_console_python()


def test_non_darwin_ignores_homebrew():
    got = pi.unimatrix_python(None, platform="linux", is_executable=lambda p: True)
    assert got == pi.resolve_console_python()
