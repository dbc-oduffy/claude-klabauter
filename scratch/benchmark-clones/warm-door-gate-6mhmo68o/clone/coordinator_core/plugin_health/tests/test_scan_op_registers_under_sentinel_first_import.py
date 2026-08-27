"""`plugin_health.scan` must register its op regardless of import order.

WHY THIS GUARD EXISTS. `scan` borrowed `_resolve_claude_home` from `sentinel` at
module scope, and the two form an import cycle. Which side lost depended purely
on who was imported first: a process that loaded `coordinator_core.ops` first
was fine, but one that loaded `coordinator_core.plugin_health.sentinel` first —
exactly what the `coordinator-doctor-sentinel` CLI trampoline does — left `scan`
half-initialised. Its `@register_op("plugin_health.scan")` decorator therefore
never ran, and the op silently did not exist for the life of that process. The
only symptom was a line on stderr saying the op would not be registered, which
is not a failure anyone reads.

That is the same shape as the defects this module's own doctor layers exist to
catch: a capability absent rather than broken, reported as nothing at all.

NEGATIVE SPEC: this test must run the import in a SUBPROCESS. Import order is a
process-global, once-only side effect — by the time any in-process test body
runs, pytest's own collection has already imported half the package, so an
in-process assertion would pass no matter which way the cycle resolved and prove
nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Imports `sentinel` FIRST, the way the sentinel CLI trampoline does, and only
#: then the op registry — the order that used to lose.
_PROBE = """
import sys
from coordinator_core.plugin_health.sentinel import main
import coordinator_core.ops
from coordinator_core.ipc import _REGISTRY
sys.stdout.write("REGISTERED" if "plugin_health.scan" in _REGISTRY else "MISSING")
"""


def test_scan_op_registers_when_sentinel_is_imported_first():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(_CLAUDE_KLABAUTER_ROOT),
        creationflags=_NO_WINDOW,
    )

    assert result.stdout.strip() == "REGISTERED", (
        "plugin_health.scan did not register its op when plugin_health.sentinel was "
        f"imported first — the import cycle is back. stderr:\n{result.stderr}"
    )
    assert "circular import" not in result.stderr, (
        f"import cycle reported on stderr:\n{result.stderr}"
    )
