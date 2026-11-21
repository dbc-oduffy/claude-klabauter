"""Scaffold-residue refusals and the accept-launcher hint in collect_fire_refusals."""

from __future__ import annotations

from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import sizing_fire as sf
from coordinator_core.ops.dispatch_emit.ask_contract import ALL_LANDED_HALT_KIND
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL as G_REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _load, _put_s
from coordinator_core.session.record_homes import record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = PurePath(record_path("", "sizings", "a.yaml")).as_posix()


@pytest.mark.spawns_process
def test_all_landed_plan_with_nothing_to_rule_resumes_to_verify(repo) -> None:  # noqa: F811
    settled = {"rows": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "waived"}]}
    _put_s(repo, requirement_register=settled)
    _landed_repo(repo, (("A1", "coded"), ("A2", "coded")))
    plan = sf.resumable_plan(_load(repo), G_REL, repo, "s")
    assert plan.incomplete == ()
    assert plan.ledger.halted == ALL_LANDED_HALT_KIND and plan.ledger.blocking == ()
