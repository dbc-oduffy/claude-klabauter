"""A token-spent gate refusal retires its halted run, so the refire it names takes a fresh run id."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import halted_run_for, write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halted_sizing_resumes import _git
from coordinator_core.warp_fire import fire_command

RUN = "warp-2026-10-01-gate-20261027T142436-989a2d"


@pytest.fixture(autouse=True)
def _git_repo(repo):  # noqa: F811
    _git(repo, "init", "-q")


def _halted(repo, run=RUN):  # noqa: F811
    rd = repo / "state" / "warp" / run
    write_halt_marker(rd, halted="refusal", sizing_rel=REL)
    return rd


def _refuse(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    return ask_gate._handler({"sizing_path": REL, "run_id": RUN, "require_live_token": True}, repo)


def test_spent_token_refusal_names_a_call_that_retires_the_run(repo):  # noqa: F811
    _halted(repo)
    out = _refuse(repo)
    assert out["halt"]["kind"] == "refusal" and fire_command(REL) in out["halt"]["reason"]
    assert "new run id" in out["halt"]["reason"]
    assert halted_run_for(repo, REL) == RUN
    assert ask_gate.retire_token_spent_halt(repo, REL) == RUN
    assert halted_run_for(repo, REL) is None


def test_halted_run_without_the_refusal_is_still_resumed(repo):  # noqa: F811
    _halted(repo)
    assert ask_gate.retire_token_spent_halt(repo, REL) is None
    assert halted_run_for(repo, REL) == RUN


def test_retirement_is_once_per_refusal(repo):  # noqa: F811
    _halted(repo)
    _refuse(repo)
    assert ask_gate.retire_token_spent_halt(repo, REL) == RUN
    assert ask_gate.retire_token_spent_halt(repo, REL) is None
    newer = "warp-2026-10-01-gate-20261027T150000-aaaaaa"
    _halted(repo, newer)
    assert halted_run_for(repo, REL) == newer
    assert ask_gate.retire_token_spent_halt(repo, REL) is None
    assert record_dir(repo, newer).is_dir()
