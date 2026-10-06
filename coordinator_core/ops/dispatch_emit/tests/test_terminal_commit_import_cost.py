"""
coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_import_cost --
cold-import cost pin for ``dispatch.terminal_commit``.

Purpose: a fresh-interpreter ``import ...terminal_commit`` must not pay for
the modules only its call paths use (``row_spans`` -> ``ceremony.git_native``,
``git.commit``, ``ops.fleet._common``), and its min-of-5 cumulative
``-X importtime`` cost stays under a ceiling.

Invariant: ``yaml`` and ``frontmatter.schema_validate`` are NOT pinned absent
-- ``commit_request`` (a top-level import the module executes) pulls both.

Negative-spec:
    - Does NOT assert an exact figure: min-of-N under a ceiling, since ambient
      load only ever adds time (pattern: telemetry/tests/test_import_ceiling.py).
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
_MODULE = "coordinator_core.ops.dispatch_emit.terminal_commit"
_DEFERRED = (
    "coordinator_core.ops.ceremony.git_native",
    "coordinator_core.ops.fleet._common",
    "coordinator_core.execute_plan_assemble.row_spans",
    "coordinator_core.git.commit",
)
_CUMULATIVE_CEILING_MS = 65.0
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


def test_deferred_modules_are_absent_after_a_fresh_import():
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
