"""Standing gate: emit-dispatch-workflow's `--plan` route under the 500ms bar, by tree process time.

Measures the CLI as the operator runs it: a cold interpreter, the imports of
`coordinator_core.ops.dispatch_emit.cli`, the dirty-write-set and gitignore git
spawns, compose and the receipt writes. In-process emit tests cannot see the
import or interpreter cost.

UNIT: process time and spawn count from `batched_process_time_ms`
(k=K_INVOCATIONS, job-object / kqueue / fork accounting), never wall clock
(CLAUDE.md, The brightline). Each reading is the min over SAMPLES samples so
peer load does not inflate it.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
Process counts are reported beside it and not asserted on; on Windows each git
spawn is two processes (child plus console host).

ADMISSION: disabled in the child env; admission is a wall-time hold by design.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI = _REPO_ROOT / "coordinator" / "bin" / "emit-dispatch-workflow.py"
_FIXTURE_PLAN = _REPO_ROOT / "coordinator_core" / "benchmarks" / "fixtures" / "emit_full_path_plan.md"
_PLAN_REL = "docs/plans/emit-full-path-plan.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    plan = repo / _PLAN_REL
    plan.parent.mkdir(parents=True)
    shutil.copyfile(_FIXTURE_PLAN, plan)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "budget@example.invalid")
    _git(repo, "config", "user.name", "budget")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture(scope="module")
def emit_measurement(tmp_path_factory):
    root = tmp_path_factory.mktemp("emit_dispatch_budget")
    repo = _build_repo(root)
    out = repo / "out" / "budget.workflow.mjs"
    out.parent.mkdir()
    cmd = [
        sys.executable, str(_CLI),
        "--plan", str(repo / _PLAN_REL),
        "--out", str(out),
        "--force",
        "--repo-root", str(repo),
    ]
    env = {**os.environ, "COORDINATOR_WORKFLOW_ADMISSION_DISABLE": "1"}
    try:
        samples = [batched_process_time_ms(cmd, k=K_INVOCATIONS, env=env) for _ in range(SAMPLES)]
    except NotImplementedError as exc:
        pytest.skip(str(exc))
    best = min(samples, key=lambda r: r["process_time_ms"])
    detail = (
        f"emit-dispatch-workflow --plan, cold tree process time: {best['process_time_ms']}ms, "
        f"procs_per_call: {best['procs_per_call']} (k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"EMIT-DISPATCH-BUDGET {detail}")
    return {"best": best, "detail": detail, "out": out}


def test_emit_dispatch_plan_route_is_under_the_500ms_bar(emit_measurement):
    m = emit_measurement
    assert m["best"]["rc"] == 0, f"CLI exited {m['best']['rc']}: {m['detail']}"
    assert m["out"].is_file(), f"no workflow script written: {m['detail']}"
    assert m["best"]["process_time_ms"] > 0, f"call cost not observed: {m['detail']}"
    assert m["best"]["process_time_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
