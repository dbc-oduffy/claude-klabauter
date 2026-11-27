"""win_safe_shlex_split keeps backslashes and rejects unbalanced quotes."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.win_argv import win_safe_shlex_split

pytestmark = pytest.mark.complete


@pytest.mark.parametrize(
    "cmd_str, expected",
    [
        ("a b  c", ["a", "b", "c"]),
        (r"C:\Users\x\tool.exe --flag", [r"C:\Users\x\tool.exe", "--flag"]),
        ('"C:\\Program Files\\x.exe" arg', ["C:\\Program Files\\x.exe", "arg"]),
        ("", []),
    ],
)
def test_split_corpus(cmd_str, expected):
    assert win_safe_shlex_split(cmd_str) == expected


@pytest.mark.parametrize("cmd_str", ['"unterminated', "'unterminated"])
def test_unbalanced_quotes_raise(cmd_str):
    with pytest.raises(ValueError):
        win_safe_shlex_split(cmd_str)


def test_module_imports_only_stdlib_leaf():
    code = (
        "import sys\n"
        "import coordinator_core.win_argv\n"
        "bad = [m for m in sys.modules if m == 'coordinator' or m.startswith('coordinator.')]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
