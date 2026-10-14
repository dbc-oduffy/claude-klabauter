"""
coordinator_core.ops.dispatch_emit.tests.test_emit_cli_import_cost --
cold-import cost pin for the ``emit-dispatch-workflow`` CLI module.

Purpose: a fresh-interpreter ``import ...dispatch_emit.cli`` must not pay for
the grind route (``grind_stages``, ``backlog_grind_assemble``,
``workstream_complete``), and its min-of-5 cumulative ``-X importtime`` cost
stays under a ceiling.

Negative-spec:
    - Does NOT assert an exact figure: min-of-N under a ceiling, since ambient
      load only ever adds time (pattern: test_terminal_commit_import_cost.py).
    - Never measures in-process: a warm ``sys.modules`` undercounts.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# cwd pins which checkout the child imports; an installed twin elsewhere on sys.path would be measured otherwise.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MODULE = "coordinator_core.ops.dispatch_emit.cli"
_DEFERRED = (
    "coordinator_core.ops.dispatch_emit.grind_stages",
    "coordinator_core.backlog_grind_assemble",
    "coordinator_core.workstream_complete",
)
# Measured min-of-5 160.2ms on 2026-10-14 (post-C1/C1b), ceiling = min + 25% headroom.
# A breach means a module-level import regrew on the CLI path.
_CUMULATIVE_CEILING_MS = 200.3
_SAMPLES = 5
_LINE_RE = re.compile(r"^import time:\s+\d+\s+\|\s+(\d+)\s+\|\s+" + re.escape(_MODULE) + r"$")


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "importtime", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )


def test_grind_route_modules_are_absent_after_a_fresh_import():
    probe = (
        f"import sys, {_MODULE}; "
        f"print('LOADED=' + ','.join(m for m in {_DEFERRED!r} if m in sys.modules))"
    )
    result = _run(probe)
    assert result.returncode == 0, result.stderr[-500:]
    assert "LOADED=" in result.stdout, result.stdout
    assert result.stdout.split("LOADED=", 1)[1].strip() == ""


def test_cumulative_import_time_is_under_the_ceiling():
    readings = []
    for _ in range(_SAMPLES):
        result = _run(f"import {_MODULE}")
        assert result.returncode == 0, result.stderr[-500:]
        for line in result.stderr.splitlines():
            m = _LINE_RE.match(line)
            if m:
                readings.append(int(m.group(1)) / 1000.0)
                break
        else:
            pytest.fail(f"no importtime line for {_MODULE}")
    assert min(readings) < _CUMULATIVE_CEILING_MS, readings
