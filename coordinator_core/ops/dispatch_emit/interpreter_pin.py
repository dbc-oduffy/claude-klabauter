"""Resolve the interpreter a target repo's targeted tests must run under, and render the prompt clause.

A `targeted_test_launcher:` key outranks the interpreter ladder: a repo-relative launcher script plus
the repo's own fixed args, run as `python <value> <test paths>`. The launcher re-execs under the
repo's pinned interpreter, so committed config never carries a host path; the engine adds no flag.

Ladder, first hit wins: `targeted_test_python:` in the repo's coordinator.local.md, then an explicit
interpreter leading `fast_test_cmd:` (absolute path or versioned name; bare python/python3 is not a
pin, and only when it runs `-m <module>`: an interpreter plus a script is a wrapper's launcher, not a pin),
then, for the claude-klabauter root only, the provisioned interpreter (one in-process pin read per process,
lazy), then none. `targeted_test_python: machine-local:<key>` resolves the key through the per-box registry, so tracked config
carries no host path. A pin is one executable: arguments or spaces resolve as "not on PATH". Pin
resolution is in-process: shutil.which and os.access.
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Union

from coordinator_core.resolve_validation_cmd import read_local_md_key

PORTABLE_CLAUSE = "Run Python as `python3`, falling back to `python` when `python3` is absent."
DEFECT_PREFIX = "interpreter-resolution-defect:"
LITERAL_PATHS_CLAUSE = (
    "Name every test path literally in the command (`pytest tests/a.py tests/b.py`), "
    "never through a shell variable such as `FILES=...; pytest $FILES`: the suite guard denies it."
)

_VERSIONED_NAME = re.compile(r"^python\d+\.\d+(\.exe)?$", re.IGNORECASE)
_ABS_INTERPRETER = re.compile(r"^python[\d.]*(\.exe)?$", re.IGNORECASE)

LAUNCHER_KEY = "targeted_test_launcher"
SOURCE_KEY = "targeted_test_python"
MACHINE_LOCAL_PREFIX = "machine-local:"
REMEDY = (
    "declare `targeted_test_python: machine-local:<key>` in coordinator.local.md "
    "(set <key> to a Python that imports the repo's packages on each box)"
)
CONFIG_FILE = "coordinator.local.md"
SOURCE_FAST_CMD = "fast_test_cmd"
SOURCE_PROVISIONED = "provisioned"
SOURCE_NONE = "none"


class TargetedPython(NamedTuple):
    """`command` is the pin as declared (None when no pin); `defect` is set when a declared pin does not resolve."""

    command: Optional[str]
    source: str
    defect: Optional[str]


def config_path(repo_root: Union[str, Path]) -> str:
    """Absolute path of the file `repo_root`'s pin is read from (the file need not exist yet)."""
    return str(Path(os.path.abspath(str(repo_root))) / CONFIG_FILE)


def remedy_for(repo_root: Union[str, Path]) -> str:
    """`REMEDY` naming the absolute config file to edit for `repo_root`; every printer of the remedy uses this."""
    return f"{REMEDY}; config file: {config_path(repo_root)}"


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
        named = bool(_ABS_INTERPRETER.match(base))
    else:
        named = bool(_VERSIONED_NAME.match(token))
    if not named or not _runs_module_or_alone(tokens[1:]):
        return None
    return token


def _runs_module_or_alone(rest: list) -> bool:
    """True when the interpreter's arguments are flags then `-m <module>` (or nothing): a script or a
    flag-argument pair is a wrapper's launcher, not the test interpreter."""
    for tok in rest:
        if tok == "-m":
            return True
        if not tok.startswith("-"):
            return False
    return not rest


def _is_absolute(token: str) -> bool:
    return os.path.isabs(token) or bool(re.match(r"^[A-Za-z]:[\\/]", token))


def _unresolved(command: str) -> Optional[str]:
    """None when `command` resolves on this host; else the reason it does not."""
    if _is_absolute(command):
        if os.path.isfile(command):
            # X_OK degrades to F_OK on Windows; the exec bit is checked only where it exists.
            if os.name != "nt":
                if os.access(command, os.X_OK):
                    return None
            else:
                return None
        return f"{command} is not an executable file on this host"
    if shutil.which(command):
        return None
    return f"{command} is not on PATH on this host"


def _machine_local_pin(ref: str) -> TargetedPython:
    """`machine-local:<key>` resolved through the in-process registry; `command` is the box's interpreter, or
    the reference itself when the key is absent or its value does not resolve (the defect names the key)."""
    from coordinator_core.machine_resolver import registry_get

    key = ref[len(MACHINE_LOCAL_PREFIX):].strip()
    value = (registry_get(key) if key else None) or ""
    value = value.strip()
    if not value:
        return TargetedPython(ref, SOURCE_KEY, f"machine-local key `{key}` is not set on this host")
    defect = _unresolved(value)
    if defect:
        return TargetedPython(ref, SOURCE_KEY, f"machine-local key `{key}`: {defect}")
    return TargetedPython(value, SOURCE_KEY, None)


def resolve_targeted_test_python(repo_root: Union[str, Path]) -> TargetedPython:
    """The repo-pinned interpreter for targeted tests, per the module ladder."""
    root = str(repo_root)
    pin = read_local_md_key(root, SOURCE_KEY).strip()
    source = SOURCE_KEY
    if pin.lower().startswith(MACHINE_LOCAL_PREFIX):
        return _machine_local_pin(pin)
    if not pin:
        pin = _leading_interpreter(read_local_md_key(root, SOURCE_FAST_CMD)) or ""
        source = SOURCE_FAST_CMD
    if not pin:
        provisioned = _provisioned_for(root)
        if provisioned:
            return TargetedPython(provisioned, SOURCE_PROVISIONED, None)
        return TargetedPython(None, SOURCE_NONE, None)
    return TargetedPython(pin, source, _unresolved(pin))


@functools.lru_cache(maxsize=1)
def _provisioned_path() -> Optional[str]:
    """The provisioned interpreter, resolved once per process (the pin is read once)."""
    from coordinator_core.install.provisioned_interpreter import resolve_provisioned_interpreter

    return resolve_provisioned_interpreter().path


def _provisioned_for(root: str) -> Optional[str]:
    """The provisioned interpreter when ``root`` is the claude-klabauter root, else None; lazy so hook paths never reach it."""
    from coordinator_core.install.provisioned_interpreter import _CLAUDE_KLABAUTER_ROOT

    try:
        if Path(root).resolve() != _CLAUDE_KLABAUTER_ROOT:
            return None
    except OSError:
        return None
    return _provisioned_path()


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
    """The prompt sentence naming the interpreter; the portable python3/python text when nothing is pinned.

    A clause that lets tests run also carries `LITERAL_PATHS_CLAUSE`; a resolution defect runs none."""
    if repo_root is None:
        return f"{PORTABLE_CLAUSE} {LITERAL_PATHS_CLAUSE}"
    launcher = _launcher_clause(repo_root)
    if launcher is not None:
        return launcher if DEFECT_PREFIX in launcher else f"{launcher} {LITERAL_PATHS_CLAUSE}"
    pinned = resolve_targeted_test_python(repo_root)
    if pinned.command is None:
        return f"{PORTABLE_CLAUSE} {LITERAL_PATHS_CLAUSE}"
    if pinned.defect is None:
        return (
            f"Run Python as `{pinned.command}` (this repo's pinned interpreter, from {pinned.source}), "
            f"never bare `python3` or `python`. {LITERAL_PATHS_CLAUSE}"
        )
    return (
        f"This repo pins `{pinned.command}` (from {pinned.source}) but it does not resolve: {pinned.defect}. "
        "Run no tests. Report status: error, tests_run: 0 and a summary starting "
        f"`{DEFECT_PREFIX}`. That is a resolution defect, not a test failure."
    )


# --- pytest capability of the chosen runner -------------------------------------------------------------
# A resolved pin proves the file exists, not that it can run the suite: a pin (or `sys.executable`) that
# lacks pytest ends a leg as "ran zero tests". Probe only the candidate actually chosen, cache the answer per
# box keyed by (realpath, mtime, size), so a steady state costs no spawn. A classifier is never a pin.
_PROBE_TIMEOUT_S = 5
_CAPABILITY_FILE = "pytest-capable-interpreters.json"
NO_PYTEST_REMEDY = "install pytest there (`<python> -m pip install pytest`) or point `targeted_test_python` at an interpreter that has it"


def _capability_cache_path() -> Path:
    from coordinator_core._settings_home import settings_home

    return settings_home() / "cache" / _CAPABILITY_FILE


def _identity(command: str) -> Optional[str]:
    try:
        real = os.path.realpath(command)
        st = os.stat(real)
    except OSError:
        return None
    return f"{real}|{st.st_mtime_ns}|{st.st_size}"


def _probe_pytest(command: str) -> bool:
    if os.path.realpath(command) == os.path.realpath(sys.executable):
        import importlib.util

        return importlib.util.find_spec("pytest") is not None
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    try:
        done = subprocess.run([command, "-c", "import pytest"], capture_output=True, timeout=_PROBE_TIMEOUT_S,
                              **no_console_creationflags())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@functools.lru_cache(maxsize=64)
def _imports_pytest(command: str) -> bool:
    """True when `command` imports pytest: in-process memo, then the per-box file cache, then one probe."""
    ident = _identity(command)
    if ident is None:
        return False
    import json

    try:
        path: Optional[Path] = _capability_cache_path()
    except (OSError, ImportError, RuntimeError):
        path = None
    table: object = {}
    if path is not None:
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            table = {}
    if isinstance(table, dict) and ident in table:
        return bool(table[ident])
    ok = _probe_pytest(command)
    if path is not None and isinstance(table, dict):
        table[ident] = ok
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(table), encoding="utf-8")
        except OSError:
            pass
    return ok


def _fallback_candidates() -> list:
    """Interpreters to try after the pin: this process's, then PATH's python3/python and `~/.local/bin`."""
    out: list = [sys.executable]
    for name in ("python3", "python"):
        for search in (None, str(Path.home() / ".local" / "bin")):
            hit = shutil.which(name, path=search)
            if hit:
                out.append(hit)
    return out


def resolve_test_runner_python(repo_root: Union[str, Path]) -> TargetedPython:
    """`resolve_targeted_test_python` whose command is also able to import pytest.

    A pin that cannot is replaced, in order, by this process's interpreter and then the PATH ones, with source
    `pytest-fallback`; none importing pytest is a defect naming the pin, the probed set and the remedy. An
    unpinned repo or an unresolved pin is returned as-is (the caller's own fallback and defect path apply)."""
    pin = resolve_targeted_test_python(repo_root)
    if not pin.command or pin.defect is not None or pin.source == SOURCE_NONE or _imports_pytest(pin.command):
        return pin
    tried = [pin.command]
    seen = {os.path.normcase(os.path.realpath(pin.command))}
    for cand in _fallback_candidates():
        key = os.path.normcase(os.path.realpath(cand))
        if key in seen:
            continue
        seen.add(key)
        tried.append(cand)
        if _imports_pytest(cand):
            return TargetedPython(cand, "pytest-fallback", None)
    return TargetedPython(pin.command, pin.source, f"it cannot import pytest and no other interpreter on this host can (tried {', '.join(tried)}); {NO_PYTEST_REMEDY}")
