"""The halted-record landing path reads the test verdict through landing_test_gate. ``land_run_halt``
lands run docs only and carries no verdict."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.git_state import head_sha
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit.tests import test_terminal_commit_halted_record as record_mod
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import repo as _record_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RECORDED_FAIL = {"targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}


def _write_fresh(root, run_id, status, *, head=None) -> None:
    path = landing_retest.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "run_id": run_id, "head": head or head_sha(root), "tests": ["t.py"], "covered": {},
            "status": status, "tests_failed": None, "at": "2026-10-21T00:00:00Z",
        }),
        encoding="utf-8",
    )


def _record_check(root) -> str:
    record_mod._write_record(root, record_mod.RUN)
    return record_mod._call(root, record_mod.LEDGER, **RECORDED_FAIL)["targeted_tests"]["check"]


SITES = [
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
