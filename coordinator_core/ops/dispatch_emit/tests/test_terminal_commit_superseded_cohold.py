"""dispatch.terminal_commit: a halted run of a superseded sizing no longer co-holds a path a peer lands; before the
supersede the same hold refuses the landing naming the halted run."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cohold_refusal, shipped_claims
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _git,
    _write,
    repo,  # noqa: F401  (fixture)
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_cohold_declared_path import (
    HOLDER,
    NEW_TEXT,
    _call,
    _holder,
    _stage_landing,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HOLDER_SIZING = "state/sizings/other.yaml"
_HOLD_REFUSALS = (cohold_refusal.REFUSED_COHOLD, "peer-hunks-co-declared")


def test_active_sizing_halted_hold_refuses_then_superseding_it_lands_the_path(repo):  # noqa: F811
    _stage_landing(repo)
    _holder(repo, "def m():\n    return 3\n")
    before = _git(repo, "rev-parse", "HEAD")

    refused = _call(repo)

    assert refused["committed"] is False, refused
    assert refused.get("refused") in _HOLD_REFUSALS, refused
    assert HOLDER in str(refused.get("error")), refused
    assert _git(repo, "rev-parse", "HEAD") == before

    _write(repo, HOLDER_SIZING, "status: superseded\n")

    landed = _call(repo)

    assert landed.get("refused") not in _HOLD_REFUSALS + (shipped_claims.SHIPPED_CLAIM_REFUSED,), landed
    assert HOLDER not in str(landed.get("error")), landed
    # The fixture's scratch run-stage file trips the downstream residue gate, which sits after every hold gate;
    # reaching it (or committing) proves the hold no longer refuses.
    if landed.get("committed"):
        assert _git(repo, "show", "HEAD:m.py") == NEW_TEXT
    else:
        assert str(landed.get("error")).startswith("residue_gate:"), landed
