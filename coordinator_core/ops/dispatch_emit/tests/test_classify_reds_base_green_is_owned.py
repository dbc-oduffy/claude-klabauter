"""A failing node that passed at base is the run's own red: classifyReds never classes it foreign."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, self_correction as sc
from coordinator_core.win_portability import no_console_creationflags

_EDITED = "coordinator_core/ops/dispatch_emit/halted_hold_release.py"
_T = "coordinator_core/ops/dispatch_emit/tests/"
_REPRO = [
    _T + "test_release_reason_once_spawns.py::test_claim_pass_over_three_halted_runs_spawns_at_most_two_git",
    _T + "test_land_run_leg_budget.py::test_halted_hold_pass_costs_one_index_walk_and_no_git_however_many_runs_hold",
    _T + "test_landing_halted_view.py::test_sibling_claim_pass_over_halted_runs_walks_the_index_once_and_spawns_no_git",
    _T + "test_residue_paths_scale.py::test_landing_residue_scan_is_bounded_at_730_withholding_runs",
]


def test_python_base_green_failing_nodes_are_owned_not_foreign():
    r = sc.classify_reds(_REPRO, [_EDITED], passed_at_base=_REPRO, reverted=[])
    assert r.owned == tuple(sorted(_REPRO))
    assert r.foreign == () and r.unverified == ()


def test_python_unmeasured_or_pre_existing_node_stays_foreign():
    nid = _REPRO[0]
    assert sc.classify_reds([nid], [_EDITED], reverted=[]).foreign == (nid,)
    assert sc.classify_reds([nid], [_EDITED], passed_at_base=[_REPRO[1]], reverted=[]).foreign == (nid,)
    r = sc.classify_reds([nid], [_EDITED], passed_at_base=[nid], pre_existing=[nid], reverted=[])
    assert r.owned == () and r.foreign == (nid,)


def _run(failing, kw):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        sc.CLASSIFY_REDS_JS
        + f"console.log(JSON.stringify(classifyReds({json.dumps(failing)}, {json.dumps([_EDITED])}, {json.dumps(kw)})));\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
def test_js_base_green_failing_nodes_are_owned_not_foreign():
    got = _run(_REPRO, {"passed_at_base": _REPRO, "reverted": []})
    assert got["owned"] == sorted(_REPRO)
    assert got["foreign"] == [] and got["unverified"] == []


@pytest.mark.spawns_process
def test_js_unmeasured_and_pre_existing_nodes_stay_foreign():
    nid = _REPRO[0]
    assert _run([nid], {"reverted": []})["foreign"] == [nid]
    assert _run([nid], {"passed_at_base": [_REPRO[1]], "reverted": []})["foreign"] == [nid]
    got = _run([nid], {"passed_at_base": [nid], "pre_existing": [nid], "reverted": []})
    assert got["owned"] == [] and got["foreign"] == [nid]


def test_review_recover_passes_passed_at_base_into_classify_reds():
    src = Path(ask_compose.__file__).read_text(encoding="utf-8")
    assert "passed_at_base: rv?.passed_at_base ?? []" in src
