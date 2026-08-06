"""test_sweep_argv.py — unit tests for the shared `parse_repo_root_argv` helper.

See coordinator/bin/lib/sweep_argv.py module docstring for the defect this
helper centralizes the fix for.
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sweep_argv import parse_repo_root_argv  # noqa: E402

_USAGE = "usage: python3 prog.py [-h] [<repo_root>]"


def test_help_returns_exit_zero_and_prints_usage(capsys):
    positional, flags, early_exit = parse_repo_root_argv([], prog="prog.py", usage=_USAGE)
    assert early_exit is None
    assert positional == []

    positional, flags, early_exit = parse_repo_root_argv(
        ["--help"], prog="prog.py", usage=_USAGE
    )
    captured = capsys.readouterr()
    assert early_exit == 0
    assert _USAGE in captured.out


def test_short_help_flag_also_exits_zero(capsys):
    _positional, _flags, early_exit = parse_repo_root_argv(["-h"], prog="prog.py", usage=_USAGE)
    assert early_exit == 0


def test_unrecognized_leading_dash_arg_rejected(capsys):
    positional, flags, early_exit = parse_repo_root_argv(
        ["--bogus"], prog="prog.py", usage=_USAGE
    )
    captured = capsys.readouterr()
    assert early_exit == 2
    assert "unrecognized argument" in captured.err
    assert "--bogus" in captured.err
    assert positional == []


def test_known_flag_recorded_not_rejected():
    positional, flags, early_exit = parse_repo_root_argv(
        ["--dry-run", "/tmp/repo"],
        prog="prog.py",
        usage=_USAGE,
        known_flags=frozenset({"--dry-run"}),
    )
    assert early_exit is None
    assert "--dry-run" in flags
    assert positional == ["/tmp/repo"]


def test_bare_positional_passes_through():
    positional, flags, early_exit = parse_repo_root_argv(
        ["/tmp/some-repo"], prog="prog.py", usage=_USAGE
    )
    assert early_exit is None
    assert positional == ["/tmp/some-repo"]
    assert flags == set()


def test_too_many_positionals_rejected(capsys):
    positional, flags, early_exit = parse_repo_root_argv(
        ["/tmp/a", "/tmp/b"], prog="prog.py", usage=_USAGE, max_positional=1
    )
    captured = capsys.readouterr()
    assert early_exit == 2
    assert "too many positional arguments" in captured.err
