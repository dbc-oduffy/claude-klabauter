from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# already has both loaded. The spawn ratchet's `_BASELINE` is shrink-only
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_LIB_DIR = Path(__file__).parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from win_argv import win_safe_shlex_split  # noqa: E402

_WINDOWS_SHAPED_PATH = "\\".join(["C:", "Users", "bob", "tools", "refresh.exe"])

CASES = [
    ("hello world", ["hello", "world"]),
    ('"hello world"', ["hello world"]),
    ("'hello world'", ["hello world"]),
    ("a b c", ["a", "b", "c"]),
    ("single", ["single"]),
    ("", []),
    ("   ", []),
    ("*.py", ["*.py"]),
    ("src/*.py test/*.py", ["src/*.py", "test/*.py"]),
    ("caf\u00e9 \u00fcnic\u00f8de", ["caf\u00e9", "\u00fcnic\u00f8de"]),
    ('"caf\u00e9 \u00fcnic\u00f8de"', ["caf\u00e9 \u00fcnic\u00f8de"]),
    (_WINDOWS_SHAPED_PATH, [_WINDOWS_SHAPED_PATH]),
    (f"{_WINDOWS_SHAPED_PATH} --flag", [_WINDOWS_SHAPED_PATH, "--flag"]),
    (f'"{_WINDOWS_SHAPED_PATH}"', [_WINDOWS_SHAPED_PATH]),
    ("a\\ b", ["a\\", "b"]),
    ("--flag=value", ["--flag=value"]),
    ("a=1 b=2", ["a=1", "b=2"]),
    ("cmd --opt 'quoted arg' trailing", ["cmd", "--opt", "quoted arg", "trailing"]),
    ('mixed "double" and \'single\'', ["mixed", "double", "and", "single"]),
    ("tab\tand\tnewline\nsplit", ["tab", "and", "newline", "split"]),
    ('"unterminated\\"', ["unterminated\\"]),
    ('"a\\\\"', ["a\\\\"]),
]


@pytest.mark.parametrize("cmd_str,expected", CASES)
def test_win_safe_shlex_split_corpus(cmd_str, expected):
    assert win_safe_shlex_split(cmd_str) == expected


@pytest.mark.parametrize(
    "cmd_str",
    [
        "'unterminated",
        '"unterminated',
        "echo 'unterminated",
        '"a\\"b"',
        'a\\"b',
        '"a\\"b" c',
        '\\"',
    ],
)
def test_win_safe_shlex_split_unbalanced_quotes_raise(cmd_str):
    with pytest.raises(ValueError):
        win_safe_shlex_split(cmd_str)


def test_win_argv_import_graph_stays_leaf_clean():
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(_LIB_DIR)!r})\n"
        "import win_argv\n"
        "names = sorted(sys.modules)\n"
        "print('\\n'.join(names))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    modules = set(result.stdout.splitlines())
    assert not any(m == "psutil" or m.startswith("psutil.") for m in modules), modules
    assert not any(
        m == "coordinator_core" or m.startswith("coordinator_core.") for m in modules
    ), modules
