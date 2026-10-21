"""The halted-run landing paths read the test verdict through landing_test_gate."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.git_state import head_sha
from coordinator_core.ops.dispatch_emit import landing_integrity, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as halt_mod
from coordinator_core.ops.dispatch_emit.tests import test_terminal_commit_halted_record as record_mod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import repo as _halt_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import repo as _record_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RECORDED_FAIL = {"targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}


def _write_fresh(root, run_id, status, *, head=None) -> None:
    path = landing_integrity.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "run_id": run_id, "head": head or head_sha(root), "tests": ["t.py"], "covered": {},
            "status": status, "tests_failed": None, "at": "2026-10-21T00:00:00Z",
        }),
        encoding="utf-8",
    )


def _halt_check(root) -> str:
    halt_mod._record(root)
    halt_mod._chunks(root, {"C1": ["done.py"]})
    params = {"script_path": "run.mjs", "run_id": halt_mod.RUN, HALTED_RECORD_PARAM: halt_mod.LEDGER, **RECORDED_FAIL}
    return land_run_halt.land(root, root, params)["targeted_tests"]["check"]


def _record_check(root) -> str:
    record_mod._write_record(root, record_mod.RUN)
    return record_mod._call(root, record_mod.LEDGER, **RECORDED_FAIL)["targeted_tests"]["check"]


SITES = [
    pytest.param(_halt_check, halt_mod.RUN, "_halt_repo", id="land_run_halt"),
    pytest.param(_record_check, record_mod.RUN, "_record_repo", id="halted_record_commit"),
]


@pytest.mark.parametrize("check,run_id,fixture", SITES)
def test_fresh_pass_at_head_clears_a_recorded_no_baseline_fail(request, check, run_id, fixture):
    root = request.getfixturevalue(fixture)
    _write_fresh(root, run_id, "pass")

    assert check(root) == "pass-on-retest"


@pytest.mark.parametrize("check,run_id,fixture", SITES)
def test_stale_fresh_verdict_leaves_the_recorded_fail_governing(request, check, run_id, fixture):
    root = request.getfixturevalue(fixture)
    _write_fresh(root, run_id, "pass", head="0" * 40)

    assert check(root) == "failed"


@pytest.mark.parametrize("check,run_id,fixture", SITES)
def test_fresh_fail_at_head_refuses(request, check, run_id, fixture):
    root = request.getfixturevalue(fixture)
    _write_fresh(root, run_id, "fail")

    assert check(root) == "failed"
