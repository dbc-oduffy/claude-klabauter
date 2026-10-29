"""Falsifier: refiring an ask whose sizing, plan and landed rows exist resumes instead of halting."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _put_s

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SETTLED = {"rows": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "waived"}]}
RUN = "warp-2026-10-21-x-73b78a-20261021T235625-cfe57e"


def test_all_landed_refire_does_not_halt(repo):  # noqa: F811
    _put_s(repo, requirement_register=SETTLED)
    _landed_repo(repo, (("A1", "coded"), ("A2", "coded")))
    rl.start(repo, RUN)
    v = gate(repo, REL, run_id=RUN)
    assert v.halt is None, f"refire over sizing + plan + landed rows halted: {v.halt}"
