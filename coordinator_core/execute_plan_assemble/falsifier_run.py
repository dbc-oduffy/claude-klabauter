"""Run a plan's recorded prime-exit-criterion falsifier and report what it observed.

Indeterminate (unparseable, unanchored, timed out, unlaunchable) is never a pass:
`passed` is None and `ran` is False.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core.execute_plan_assemble.falsifier_shape import falsifier_how_unanchored
from coordinator_core.win_portability import no_console_creationflags

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_OUTPUT_CAP = 4000


@dataclass(frozen=True)
class FalsifierObservation:
    """`ran` is True only when the command exited by itself; `passed` is
    `exit_code == 0` then, else None (indeterminate)."""

    ran: bool
    exit_code: Optional[int]
    output: str
    passed: Optional[bool]


def _indeterminate(note: str) -> FalsifierObservation:
    return FalsifierObservation(ran=False, exit_code=None, output=note, passed=None)


def _argv(how: str) -> Optional[list]:
    match = _BACKTICK_RE.search(how)
    if match is None:
        return None
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        return None
    if not argv:
        return None
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv


def run_recorded_falsifier(how: str, *, repo_root: Path, timeout_s: float) -> FalsifierObservation:
    """Run the backticked command in `how` (no shell, cwd=repo_root). Never raises."""
    if not isinstance(how, str):
        return _indeterminate("falsifier how is not a string")
    if falsifier_how_unanchored(how) is not None:
        return _indeterminate("falsifier how decides on an unanchored substring")
    argv = _argv(how)
    if argv is None:
        return _indeterminate("no parseable backticked command in falsifier how")
    # Trap: this one spawn is the plan's own observation, not engine work, so it
    # is not held to the engine spawn budget. The child runs an arbitrary plan
    # command (pytest spawns git and interpreters), so it is not a leaf:
    # no_console_creationflags, never leaf_spawn_creationflags (DETACHED_PROCESS
    # would pop a console per grandchild).
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _indeterminate("falsifier timed out")
    except (OSError, ValueError) as exc:
        return _indeterminate(f"falsifier could not launch: {exc}")
    output = ((proc.stdout or "") + (proc.stderr or ""))[-_OUTPUT_CAP:]
    return FalsifierObservation(
        ran=True, exit_code=proc.returncode, output=output, passed=proc.returncode == 0
    )
