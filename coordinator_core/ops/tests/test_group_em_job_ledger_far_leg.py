"""
coordinator_core.ops.tests.test_group_em_job_ledger_far_leg -- the job-ledger ops are classified
read-only / compute-only, so the far-box leg is admitted by the remote_safe gate and returns rows.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import op_scopes
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.group_em import box_registry, box_transport
from coordinator_core.ops import group_em_job_ledger as op
from coordinator_core.remote_engine import policy

INVOKE = "/opt/far-settings-home/bin/coordinator-invoke"
BOX = {"box_id": "box-far", "transport": {"kind": "ssh", "host": "far.example", "invoke": INVOKE}}
_NAMES = ("groupem.job_ledger", "groupem.job_ledger_local")


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(box_registry, "lock_dir", lambda home=None: tmp_path / "box-locks")
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(BOX)])
    box_transport._reset_limits()


@pytest.mark.parametrize("name", _NAMES)
def test_classified_compute_only_and_remote_safe(name: str) -> None:
    assert classify(name) is OpClass.COMPUTE_ONLY
    assert policy.remote_safe(name) is True
    assert box_transport._op_allowed(name) is True
    assert op_scopes._OP_KEY_SCOPE[name] == "none"


def test_a_mutating_sibling_stays_refused_on_the_far_box() -> None:
    assert box_transport._op_allowed("groupem.fleet_book") is False
    assert classify("groupem.job_progress") is OpClass.MUTATING
    assert box_transport._op_allowed("groupem.job_progress") is False


def test_far_leg_returns_rows_through_the_real_transport() -> None:
    row = {"box_id": "box-far", "state": "ok", "sampled_at": 1.0, "flagged": 1, "unavailable": {},
           "jobs": [{"pid": 9, "flags": ["unbooked"]}]}
    seen: list = []

    def run(argv, timeout):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": row}), "")

    far = op._far_row(BOX, 2.0, lambda rec, name, params, *, timeout: box_transport.invoke_remote(
        rec, name, params, run, timeout=timeout))
    assert far["state"] == "ok" and far["jobs"] == row["jobs"] and far["flagged"] == 1
    assert seen[0][6].startswith(INVOKE + " groupem.job_ledger_local")
