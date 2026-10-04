"""The `orient_brief` import closure stays lean: no assembler monolith, no IPC/invoke stack.

Mirrors `test_pickup_brief_import_closure.py`. The 45ms interpreter-plus-envelope floor in the
brightline budget holds only while these stay out of `sys.modules`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_NO_CONSOLE = no_console_creationflags()

_FORBIDDEN = (
    "coordinator_core.orient_assemble",
    "coordinator_core.pickup_assemble",
    "coordinator_core.review_assemble",
    "coordinator_core.merge_assemble",
    "coordinator_core.consolidate_assemble",
    "coordinator_core.quick_wrap_assemble",
    "coordinator_core.backlog_grind_assemble",
    "coordinator_core.ipc",
    "coordinator_core.invoke",
)

_PROBE = """
import sys
sys.path.insert(0, {engine_root!r})
from coordinator_core import orient_brief as ob
if {run_brief!r}:
    try:
        ob.main(["brief", "--cadence", "day"])
    except SystemExit:
        pass
forbidden = {forbidden!r}
hits = sorted(n for n in sys.modules if any(n == f or n.startswith(f + ".") for f in forbidden))
print("FORBIDDEN=" + repr(hits))
"""


def _loaded_forbidden(run_brief: bool) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(
                engine_root=str(_ENGINE_ROOT), run_brief=run_brief, forbidden=_FORBIDDEN
            ),
        ],
        cwd=str(_ENGINE_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        **_NO_CONSOLE,
    )
    assert "FORBIDDEN=" in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout.split("FORBIDDEN=", 1)[1].splitlines()[0]


def test_module_import_pulls_no_heavy_module():
    assert _loaded_forbidden(False) == "[]"


def test_brief_verb_pulls_no_heavy_module():
    assert _loaded_forbidden(True) == "[]"
