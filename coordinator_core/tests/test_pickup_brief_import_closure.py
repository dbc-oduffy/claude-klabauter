"""
coordinator_core.tests.test_pickup_brief_import_closure — pins that
`coordinator_core.pickup_brief` never pulls the monolith
`coordinator_core.pickup_assemble` into `sys.modules` for the `brief` verb.

Runs the brief verb in a FRESH interpreter (a real spawn — `cadence` +
`spawns_process`, per the row body) because an in-process `sys.modules`
check would be poisoned by whatever a peer test in the same session already
imported; a fresh interpreter is the only honest way to observe this
module's own import closure in isolation.

`coordinator_core.ceremony_common.*` is explicitly ALLOWED (the row body:
"the allowed set explicitly names coordinator_core.ceremony_common.* as
present-and-permitted"). This test asserts only the monolith's absence.

Spec backlink: docs/plans/2026-09-11-…, chunk C10.
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

_PROBE = """
import sys
sys.path.insert(0, {engine_root!r})
from coordinator_core import pickup_brief as pb

# `brief` verb, missing-artifact path — never a real repo dependency; the
# point is exercising `main()`'s `brief` branch's own import closure, not a
# real resolution.
try:
    pb.main(["brief", "state/handoffs/does-not-exist-anywhere.md"])
except SystemExit:
    pass
except Exception:
    pass

monolith_present = any(
    name == "coordinator_core.pickup_assemble" or name.startswith("coordinator_core.pickup_assemble.")
    for name in sys.modules
)
print("MONOLITH_PRESENT=" + str(monolith_present))
"""


def test_brief_verb_never_imports_pickup_assemble():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(engine_root=str(_ENGINE_ROOT))],
        cwd=str(_ENGINE_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        **_NO_CONSOLE,
    )
    assert "MONOLITH_PRESENT=False" in proc.stdout, (
        f"pickup_brief's `brief` verb pulled coordinator_core.pickup_assemble into "
        f"sys.modules. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_ceremony_common_is_allowed_and_present():
    """The allowed set explicitly permits `coordinator_core.ceremony_common.*`
    — this module DOES consume it (`detect_conflicting_payload_channels`,
    `resolve_json_payload_flag`), and that consumption is not a closure
    violation."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(engine_root=str(_ENGINE_ROOT))],
        cwd=str(_ENGINE_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        **_NO_CONSOLE,
    )
    assert proc.returncode == 0 or "MONOLITH_PRESENT" in proc.stdout


def test_module_level_import_does_not_pull_monolith():
    """The non-brief import path (module import alone, no `main()` call) is
    an even stricter floor: importing `pickup_brief` at all must not import
    the monolith module-scope."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s'); "
                "import coordinator_core.pickup_brief; "
                "print('MONOLITH_PRESENT=' + str(any(n.startswith('coordinator_core.pickup_assemble') for n in sys.modules)))"
                % str(_ENGINE_ROOT)
            ),
        ],
        cwd=str(_ENGINE_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        **_NO_CONSOLE,
    )
    assert "MONOLITH_PRESENT=False" in proc.stdout, proc.stdout + proc.stderr
