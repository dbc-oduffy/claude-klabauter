"""A carried fail count naming no node yields to the run's current green test-leg.json, never a phantom red."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-leg"
#: The witness shape: the overlay-inconclusive fold carries one failure with no node and no baseline.
CARRIED = {"targeted_tests": {"status": "fail", "tests_failed": 1, "tests_run": 31, "baseline": None}}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return root


def _leg(root: Path, **fields) -> None:
    path = measured_rev_record_path(root, RUN_ID).parent / "test-leg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_green_current_leg_clears_a_nodeless_carried_fail(repo):
    _leg(repo, status_after="pass", status="pass", tests_run=31, tests_failed=0, failing=[])
    findings, check = li.landing_test_gate(repo, CARRIED, RUN_ID)
    assert check == "pass-on-retest" and not findings


def test_no_leg_record_still_refuses_the_carried_fail(repo):
    findings, check = li.landing_test_gate(repo, CARRIED, RUN_ID)
    assert check == "failed" and findings


@pytest.mark.parametrize("leg", [
    {"status": "pass", "tests_run": 0, "failing": []},
    {"status": "fail", "tests_run": 31, "failing": ["tests/test_x.py::test_red"]},
])
def test_non_green_leg_never_clears_it(repo, leg):
    _leg(repo, **leg)
    findings, check = li.landing_test_gate(repo, CARRIED, RUN_ID)
    assert check != "pass-on-retest"
