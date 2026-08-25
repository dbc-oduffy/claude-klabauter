"""test_sweep_argv_shared.py — cross-CLI parity coverage for the shared
`sweep_argv.parse_repo_root_argv` leading-dash-argument guard
(coordinator/bin/lib/sweep_argv.py).

Every CLI parametrized here (`sweep-actioned-memos.py`, `sweep-boot.py`)
previously either took `argv[0]`
unconditionally as the repo-root positional, or fell through to treating any
unrecognized leading-dash token as a positional — both shapes forwarded `--help`
(or a typo'd flag) downstream as a bogus repo-root value instead of rejecting it.
This file asserts the fix closes the class, not just the one originally-reported
instance (sweep-actioned-memos.py) — new CLIs adopting the shared helper get
this coverage for free by being added to `_CLI_SCRIPTS` below.

Runs each CLI as a real subprocess with `--help` / an unrecognized flag only —
both paths return before any transport/repo-root resolution work, so this is
safe to run for real (no repo mutation, no network, no git dependency).

Spec backlink: coordinator/bin/lib/sweep_argv.py
Spec backlink: coordinator/bin/sweep-actioned-memos.py `_resolve_repo_root`
fail-silent-success fix, 2026-07-25.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_CLI_SCRIPTS = [
    "sweep-actioned-memos.py",
    "sweep-boot.py",
]


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script), *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


@pytest.mark.parametrize("script", _CLI_SCRIPTS)
def test_help_exits_zero(script):
    proc = _run(script, "--help")
    assert proc.returncode == 0, f"{script} --help: rc={proc.returncode}, stderr={proc.stderr!r}"


@pytest.mark.parametrize("script", _CLI_SCRIPTS)
def test_unrecognized_flag_exits_nonzero(script):
    proc = _run(script, "--this-flag-does-not-exist")
    assert proc.returncode != 0, (
        f"{script} --this-flag-does-not-exist: expected non-zero exit "
        f"(rejecting an unrecognized flag), got rc=0 -- fail-silent-success regression"
    )
    assert "unrecognized argument" in proc.stderr
    assert "--this-flag-does-not-exist" in proc.stderr
