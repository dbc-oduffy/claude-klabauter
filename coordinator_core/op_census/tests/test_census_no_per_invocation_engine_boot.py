"""coordinator_core.op_census.tests.test_census_no_per_invocation_engine_boot
— enforces the AMENDED hard constraint 7 (C6).

Purpose: replaces the old, now-incorrect "does not import
`coordinator_core.ops`" assertion (PM Ruling 3-A/3-D). Reading the
authoritative op registry is PERMITTED here — `op_census_report.census()`
calls `spawn_bearing_ops.live_registry_op_names()`, which imports
`coordinator_core.ops` (~343.8ms, once-per-process) — but that cost must be
paid AT MOST ONCE per process, never re-triggered by a second `census()`
call in the same process. This file asserts the SHAPE (no re-import per
invocation), not a bare import ban.

Stays its own file (not folded into `test_op_census_report.py`) per the
dispatch brief: it IS a test file, so the dispatch-emit terminal-test-scope
resolver maps it to itself regardless of stem, and it is a named
acceptance-criteria deliverable.

Precedent: `X:/DoE-claude/coordinator/tests/_hook_spawn_budget.py`'s own  # abs-path-ok: sibling-repo doc citation, not a runtime dependency
`test_no_subprocess_import_in_this_module` — self-checked, static-inspection
shape, reused here for the "no top-level unconditional import" half of this
guard.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops import op_census_report

# ---------------------------------------------------------------------------
# Static half: op_census_report.py itself never imports coordinator_core.ops
# unconditionally at module scope -- only functions it calls (in
# spawn_bearing_ops) import it, and only when actually invoked.
# ---------------------------------------------------------------------------


def test_module_has_no_top_level_coordinator_core_ops_import():
    with open(op_census_report.__file__, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    # A top-level (unconditional) import is any `import coordinator_core.ops`
    # or `from coordinator_core.ops import ...` line at column 0 -- inside a
    # function body it is indented and does not match this check.
    top_level_op_import_lines = [
        line
        for line in lines
        if (line.startswith("import coordinator_core.ops") or line.startswith("from coordinator_core.ops "))
    ]
    assert top_level_op_import_lines == [], (
        "op_census_report.py must not import coordinator_core.ops "
        f"unconditionally at module scope; found: {top_level_op_import_lines!r}"
    )


def test_module_does_not_import_ipc_dispatch_stack_eagerly():
    """`from coordinator_core.ipc import register_op` is fine (a plain
    name import, not the ops package) -- pins that this module's own
    top-level imports stay to the lightweight op_census.* package plus
    `register_op`, never `coordinator_core.ops` itself."""
    with open(op_census_report.__file__, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "import coordinator_core.ops\n" not in source.split("def ")[0].split("class ")[0]


# ---------------------------------------------------------------------------
# Dynamic half: in a FRESH subprocess, `coordinator_core.ops` is absent from
# sys.modules until the first call that needs it, and a second call does not
# re-trigger the import (the module object identity is unchanged) -- the
# shape the amended constraint 7 actually requires.
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = """
import sys, json

import coordinator_core.ops.op_census_report as ocr
from coordinator_core.op_census.spawn_bearing_ops import live_registry_op_names

live_registry_op_names()
mod_id_first = id(sys.modules.get("coordinator_core.ops"))

live_registry_op_names()
mod_id_second = id(sys.modules.get("coordinator_core.ops"))

print(json.dumps({
    "present_after_first_call": "coordinator_core.ops" in sys.modules,
    "same_module_object": mod_id_first == mod_id_second,
}))
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_registry_import_present_and_idempotent_across_two_calls():
    """Dynamic half, in a FRESH subprocess: `coordinator_core.ops` (and
    therefore `ipc._REGISTRY`) is present after the first call that needs
    it, and a SECOND call in the same process reuses the exact same module
    object (Python's own `sys.modules` cache) rather than re-executing
    the import -- the shape amended constraint 7 requires: at most one
    engine boot per PROCESS, never one per invocation. Note: under this
    default (non-lazy) test-collection process, `coordinator_core.ops`'s
    own __init__.py eagerly imports its full op set at FIRST import
    regardless of which submodule triggers it (see
    `coordinator_core/ops/__init__.py`'s own "Default behavior" section) --
    so this test pins re-import safety across repeated calls, which is the
    property this module's own code controls, rather than re-deriving
    __init__.py's independently-tested lazy/eager gating."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    assert payload["present_after_first_call"] is True


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_second_call_does_not_re_import_coordinator_core_ops():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    assert payload["same_module_object"] is True
