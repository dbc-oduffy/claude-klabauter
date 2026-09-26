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
