"""
coordinator_core.ops.tests.test_detect_staged_rollback_import_graph

C2 (state/dispatch-briefs/2026-08-25-the-commit-gate-stops-importing-a-
subsystem/C2.md): AC3, structurally pinned -- importing
``coordinator_core.ops.detect_staged_rollback`` must not TRANSITIVELY pull in
the HEAVY bash_guards subsystem (``coordinator_core.bash_guards._helpers``,
and anything ``_helpers`` itself drags in) or
``coordinator_core.subagent_sandbox`` (any submodule). A grep of the import
line at the top of ``detect_staged_rollback.py`` is not sufficient -- the
defect this pins against is transitivity: even an import that itself reads
``from coordinator_core.bash_guards._override_doc import ...`` (the leaf,
no imports of its own) could still drag the wider subsystem in if some OTHER
module on the import path did the eager importing instead.

The bare ``coordinator_core.bash_guards`` package and the
``coordinator_core.bash_guards._override_doc`` leaf ARE expected present --
the leaf's own surface lives inside that package (see C2's brief, "Surface:
coordinator_core/bash_guards/_override_doc.py") and importing any submodule
necessarily registers its parent package in ``sys.modules``; asserting their
absence would fail unconditionally regardless of this chunk's fix. What must
be absent is ``coordinator_core.bash_guards._helpers`` (the module this
chunk stopped importing) and every ``coordinator_core.subagent_sandbox.*``
entry -- C3's ``heavy_modules_expected_absent`` row names the identical two
targets, so this test and that ratchet pin the same fact from two angles.

Run in a subprocess (not the current interpreter) so ``sys.modules`` reflects
ONLY what importing this op actually loads -- the test process's own prior
imports (this test file, pytest, conftest fixtures) must not contaminate the
measurement.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

#: The two modules this chunk's fix removed from the transitive import graph
#: of `coordinator_core.ops.detect_staged_rollback` -- identical to C3's
#: `heavy_modules_expected_absent` row for this same target.
_PROBE = (
    "import sys\n"
    "import coordinator_core.ops.detect_staged_rollback\n"
    "disallowed_exact = {'coordinator_core.bash_guards._helpers', 'coordinator_core.subagent_sandbox'}\n"
    "names = sorted(\n"
    "    m for m in sys.modules\n"
    "    if m in disallowed_exact\n"
    "    or m.startswith('coordinator_core.subagent_sandbox.')\n"
    ")\n"
    "print('\\n'.join(names))\n"
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_does_not_pull_in_helpers_or_subagent_sandbox():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, (
        f"probe subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert leaked == [], (
        "importing coordinator_core.ops.detect_staged_rollback pulled in "
        f"disallowed sys.modules entries: {leaked}"
    )
