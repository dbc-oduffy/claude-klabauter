"""A ledger-resume refire whose declared product HEAD already holds from a pre-halt commit naming the
plan answers ``already_landed`` with that sha; a verdict crediting another commit is still refused."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_pre_halt_credit_satisfies_since_halt import (
    _terminal,
    _verdict,
)
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    _composed,
    _git,
    _refire,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _carrier(root) -> str:
    """The pre-halt checkpoint commit that carries `a.py` and names the plan."""
    return _git(root, "log", "-1", "--format=%H", "--", "a.py").strip()


def test_product_landed_before_the_halt_answers_already_landed(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict([]))

    assert out["committed"] is True and out[land_run.ALREADY_LANDED_KEY] is True, out
    assert out["sha"] == _carrier(halted) and out.get("refused") is None and out.get("error") is None, out
    assert "a.py" in out["paths"]
    assert _git(halted, "rev-parse", "HEAD") == head


def test_verdict_crediting_another_commit_is_still_refused(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict(["abcdef1234"]))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert land_run.ALREADY_LANDED_KEY not in out
    assert _git(halted, "rev-parse", "HEAD") == head
