"""Resolve the interpreter a target repo's targeted tests must run under, and render the prompt clause.

A `targeted_test_launcher:` key outranks the interpreter ladder: a repo-relative launcher script plus
the repo's own fixed args, run as `python <value> <test paths>`. The launcher re-execs under the
repo's pinned interpreter, so committed config never carries a host path; the engine adds no flag.

Ladder, first hit wins: `targeted_test_python:` in the repo's coordinator.local.md, then an explicit
interpreter leading `fast_test_cmd:` (absolute path or versioned name; bare python/python3 is not a
pin), then none. A pin is one executable: arguments or spaces resolve as "not on PATH". In-process only: shutil.which and os.access, never a spawn.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import NamedTuple, Optional, Union

from coordinator_core.resolve_validation_cmd import read_local_md_key

PORTABLE_CLAUSE = "Run Python as `python3`, falling back to `python` when `python3` is absent."
DEFECT_PREFIX = "interpreter-resolution-defect:"

_VERSIONED_NAME = re.compile(r"^python\d+\.\d+(\.exe)?$", re.IGNORECASE)
_ABS_INTERPRETER = re.compile(r"^python[\d.]*(\.exe)?$", re.IGNORECASE)

LAUNCHER_KEY = "targeted_test_launcher"
SOURCE_KEY = "targeted_test_python"
SOURCE_FAST_CMD = "fast_test_cmd"
SOURCE_NONE = "none"


class TargetedPython(NamedTuple):
    """`command` is the pin as declared (None when no pin); `defect` is set when a declared pin does not resolve."""

    command: Optional[str]
    source: str
    defect: Optional[str]


def _leading_interpreter(fast_test_cmd: str) -> Optional[str]:
    """The first token of `fast_test_cmd` when it is an explicit interpreter, else None."""
    try:
        tokens = shlex.split(fast_test_cmd, posix=os.name != "nt")
    except ValueError:
        tokens = fast_test_cmd.split()
    if not tokens:
        return None
    token = tokens[0].strip("\"'")
    if _is_absolute(token):
        base = re.split(r"[\\/]", token)[-1]
        return token if _ABS_INTERPRETER.match(base) else None
    return token if _VERSIONED_NAME.match(token) else None


def _is_absolute(token: str) -> bool:
    return os.path.isabs(token) or bool(re.match(r"^[A-Za-z]:[\\/]", token))


def _unresolved(command: str) -> Optional[str]:
    """None when `command` resolves on this host; else the reason it does not."""
    if _is_absolute(command):
        if os.path.isfile(command) and os.access(command, os.X_OK):
            return None
        return f"{command} is not an executable file on this host"
    if shutil.which(command):
        return None
    return f"{command} is not on PATH on this host"


def resolve_targeted_test_python(repo_root: Union[str, Path]) -> TargetedPython:
    """The repo-pinned interpreter for targeted tests, per the module ladder."""
    root = str(repo_root)
    pin = read_local_md_key(root, SOURCE_KEY).strip()
    source = SOURCE_KEY
    if not pin:
        pin = _leading_interpreter(read_local_md_key(root, SOURCE_FAST_CMD)) or ""
        source = SOURCE_FAST_CMD
    if not pin:
        return TargetedPython(None, SOURCE_NONE, None)
    return TargetedPython(pin, source, _unresolved(pin))


def _launcher_clause(repo_root: Union[str, Path]) -> Optional[str]:
    """The launcher sentence when `targeted_test_launcher:` is declared, else None."""
    value = read_local_md_key(str(repo_root), LAUNCHER_KEY).strip()
    if not value:
        return None
    script = value.split()[0]
    if _is_absolute(script) or ".." in re.split(r"[\\/]", script):
        reason = f"{script} is not a repo-relative path"
    elif not (Path(repo_root) / script).is_file():
        reason = f"{script} does not exist in this repo"
    else:
        return (
            f"Run targeted tests as `python {value} <test paths>` (this repo's launcher, from "
            f"{LAUNCHER_KEY}), never bare pytest or another interpreter."
        )
    return (
        f"This repo declares `{LAUNCHER_KEY}: {value}` but it does not resolve: {reason}. "
        "Run no tests. Report status: error, tests_run: 0 and a summary starting "
        f"`{DEFECT_PREFIX}`. That is a resolution defect, not a test failure."
    )


def interpreter_clause(repo_root: Union[str, Path, None]) -> str:
    """The prompt sentence naming the interpreter; the portable python3/python text when nothing is pinned."""
    if repo_root is None:
        return PORTABLE_CLAUSE
    launcher = _launcher_clause(repo_root)
    if launcher is not None:
        return launcher
    pinned = resolve_targeted_test_python(repo_root)
    if pinned.command is None:
        return PORTABLE_CLAUSE
    if pinned.defect is None:
        return (
            f"Run Python as `{pinned.command}` (this repo's pinned interpreter, from {pinned.source}), "
            "never bare `python3` or `python`."
        )
    return (
        f"This repo pins `{pinned.command}` (from {pinned.source}) but it does not resolve: {pinned.defect}. "
        "Run no tests. Report status: error, tests_run: 0 and a summary starting "
        f"`{DEFECT_PREFIX}`. That is a resolution defect, not a test failure."
    )
