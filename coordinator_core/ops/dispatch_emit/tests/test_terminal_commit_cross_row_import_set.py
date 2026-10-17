"""
coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_cross_row_import_set --
the plan-run ``dispatch.terminal_commit`` full path never loads ``asyncio``.

Purpose: in a fresh interpreter, after the benchmark's own plan-run fixture runs end to end
(``build_fixture`` + ``_prepare_close`` + ``_run_full_path``), ``asyncio`` and the modules that
only existed to serve it (event-loop transports, the async-only handoff closure, the leak gate's
deferred dependencies) are absent from ``sys.modules``. Rows C1 to C11 each own one importer; this
test is the closure over all of them. Plan 2026-10-17-terminal-commit-under-200ms (K1) appends
``baton_assemble`` and ``pickup_assemble``; ``session.claims``, ``spine_read``,
``schema_validate`` and ``plan_tasks_render`` stay loaded by other path modules and are not pinned.

Negative-spec:
    - Does NOT copy the benchmark driver: it is imported.
    - Never drops ``asyncio`` from ``_ABSENT``: a reappearing importer is found with
      ``python -m coordinator_core.benchmarks.terminal_commit_ab --asyncio-importers``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ABSENT = (
    "asyncio",
    "concurrent.futures",
    *(("selectors",) if sys.platform == "win32" else ()),
    "socket",
    "ssl",
    "coordinator_core.authoring_leaks.gate",
    "coordinator_core.ops.session.record_pickup",
    "coordinator_core.ops.handoff_children",
    "coordinator_core.baton_assemble",
    "coordinator_core.pickup_assemble",
)

_PROBE = """
import json, sys
from pathlib import Path
from coordinator_core.benchmarks import terminal_commit_full_path as drv
repo, script = drv.build_fixture(Path(sys.argv[1]))
drv._prepare_close(repo)
out = drv._run_full_path(repo, script)
ok = out.get("review_stamp") == "minted" and out.get("plan_status") == "implemented"
print("RESULT=" + json.dumps({"ok": ok, "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules]}))
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_plan_run_full_path_never_loads_asyncio(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path), json.dumps(_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["ok"], "full path did not complete; an import-set reading of a failed run proves nothing"
    assert got["loaded"] == []
