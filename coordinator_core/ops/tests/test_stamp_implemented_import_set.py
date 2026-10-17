"""
coordinator_core.ops.tests.test_stamp_implemented_import_set -- import-set pin for
the ``cs_stamp_plan_implemented`` flip that ``dispatch.terminal_commit`` runs.

Purpose: the flip refuses a plan with open spine rows and stamps it implemented
without loading the ``workstream_complete`` package (tens of ms of directive
modules the stamp never calls).

Negative-spec:
    - Does NOT change what the flip refuses or writes: the refusal text and the
      on-disk status are asserted alongside the import set.
    - Never measures in-process: a warm ``sys.modules`` hides the load.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

# cwd pins which checkout the child imports; an installed twin elsewhere on sys.path would be measured otherwise.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ABSENT = (
    "coordinator_core.workstream_complete",
    "coordinator_core.ops.fleet.archive_plans",
    "coordinator_core.execute_plan_assemble.close_out_and_stamp",
)

_CHILD = textwrap.dedent(
    """
    import contextlib, io, json, subprocess, sys
    from pathlib import Path

    from coordinator_core.benchmarks.terminal_commit_full_path import PLAN_REL, build_fixture

    repo, _ = build_fixture(Path(sys.argv[1]))
    plan = repo / PLAN_REL
    if sys.argv[2] == "coded":
        plan.write_text(plan.read_text(encoding="utf-8").replace("disposition: open", "disposition: coded"), encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "coded"], cwd=repo, check=True)
    from coordinator_core.archive_stamp import cs_stamp_plan_implemented

    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cs_stamp_plan_implemented(
            str(plan), falsifier_verdict="pass", falsifier_output="x", prose="p",
            refuse_open_spine_rows=True, commit=False, ship_sha="a" * 40,
        )
    print(json.dumps({
        "rc": rc,
        "stderr": err.getvalue(),
        "status": [l for l in plan.read_text(encoding="utf-8").splitlines() if l.startswith("status:")],
        "loaded": sorted(m for m in sys.modules if m.startswith(tuple(sys.argv[3:]))),
    }))
    """
)


def _run(tmp_path: Path, disposition: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), disposition, *_ABSENT],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_flip_of_a_coded_plan_stamps_implemented_without_workstream_complete(tmp_path: Path) -> None:
    out = _run(tmp_path, "coded")
    assert out["rc"] == 0, out["stderr"]
    assert out["status"] == ["status: implemented"]
    assert out["loaded"] == []


def test_open_spine_row_refusal_names_the_row_without_workstream_complete(tmp_path: Path) -> None:
    out = _run(tmp_path, "open")
    assert out["rc"] == 1
    assert "1 spine row(s) still open: C3" in out["stderr"]
    assert out["status"] == ["status: executing"]
    assert out["loaded"] == []


@pytest.mark.parametrize(
    "name",
    [
        "_evaluate_goal_falsifier_gate",
        "_goal_refusal_next_move",
        "_ac_table_desync_finding",
        "_ac_advisory_text",
        "_verify_baseline_ref",
        "_read_status_override",
    ],
)
def test_close_out_re_exports_the_goal_gate_definitions(name: str) -> None:
    from coordinator_core.execute_plan_assemble import close_out_and_stamp, goal_gate

    assert getattr(close_out_and_stamp, name) is getattr(goal_gate, name)


@pytest.mark.parametrize("disposition", ["coded", "open", "waived-nonsense"])
def test_open_spine_row_ids_agree_with_the_gate(tmp_path: Path, disposition: str) -> None:
    from coordinator_core.archive_stamp import _open_spine_row_ids
    from coordinator_core.benchmarks.terminal_commit_full_path import PLAN_REL, build_fixture
    from coordinator_core.workstream_complete.directives_spine_worklist import compute_open_spine_row_gate

    repo, _ = build_fixture(tmp_path)
    plan = repo / PLAN_REL
    plan.write_text(plan.read_text(encoding="utf-8").replace("disposition: open", f"disposition: {disposition}"), encoding="utf-8")
    gate = compute_open_spine_row_gate(plan.stem, plan)
    expected = [r.id for r in gate.rows] if gate.verdict == "applicable" else []
    assert _open_spine_row_ids(plan) == expected
