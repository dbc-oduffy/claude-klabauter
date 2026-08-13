"""test_win_argv_leaf — pytest coverage for the shared Windows-safe argv
tokenizer, `coordinator/bin/lib/win_argv.py`.

Spec backlink: state/handoffs/2026-08-06-windows-safe-tokenizer-consolidation.md
— option (c) (consolidate into a leaf module with no transitive weight).
This is the AC2/AC5 mechanism: a real parametrized corpus over the shared
implementation (so a future edit to the tokenizer can no longer drift
between two copies, because there is only one), plus an import-graph guard
(AC1/AC4) that fails the build if `win_argv` ever stops being leaf-clean —
the whole reason the shared implementation was allowed to exist without
paying the ceremony hook's hot-path import budget
(`coordinator_core/benchmarks/import-budget-manifest.json`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from win_argv import win_safe_shlex_split  # noqa: E402

# POSIX filesystems treat `\` as an ordinary filename character, not a
# separator — a Windows-shaped path built from segments joined with
# backslashes is a faithful stand-in for a real Windows drive-letter path,
# without hardcoding a platform-specific literal (matches the style in
# test_refresh_plugin_argv_contract.py).
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
    ("a\\ b", ["a\\", "b"]),  # negative-spec: escaped-space is NOT one token
    ("--flag=value", ["--flag=value"]),
    ("a=1 b=2", ["a=1", "b=2"]),
    ("cmd --opt 'quoted arg' trailing", ["cmd", "--opt", "quoted arg", "trailing"]),
    ('mixed "double" and \'single\'', ["mixed", "double", "and", "single"]),
    ("tab\tand\tnewline\nsplit", ["tab", "and", "newline", "split"]),
    # divergence-from-shlex.split pins (docstring's "Once a backslash is
    # present" paragraph): backslash is a literal char here, never an
    # escape, so `"unterminated\"` is a balanced quoted token — shlex.split
    # raises ValueError on the same input instead.
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
        '"a\\"b"',  # backslash not consumed as escape -> mid-token quote
        'a\\"b',  # same divergence unquoted
        '"a\\"b" c',  # shlex.split returns ['a"b', 'c']; we raise
        '\\"',  # shlex.split returns ['"']; we raise
    ],
)
def test_win_safe_shlex_split_unbalanced_quotes_raise(cmd_str):
    with pytest.raises(ValueError):
        win_safe_shlex_split(cmd_str)


def test_win_argv_import_graph_stays_leaf_clean():
    """Import `win_argv` in a clean subprocess interpreter and assert its
    transitive module delta is bounded: `shlex` plus its own stdlib chain
    only. Neither `coordinator_core*` nor `psutil` may appear in
    `sys.modules` afterwards — that is the specific trap this leaf module
    exists to avoid (see the module docstring's negative-spec)."""
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
