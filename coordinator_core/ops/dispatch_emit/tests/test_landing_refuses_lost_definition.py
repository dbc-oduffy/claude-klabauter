"""A landed caller of a module attribute defined only by an uncommitted file the landing leaves out is lost-definition."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li

pytestmark = [pytest.mark.complete]  # repo setup only; the calls under test are spawn-guarded

# (module file, attribute the worktree adds, caller source) -- the three incidents.
INCIDENTS = [
    ("pkg/run_token.py", "redeem_claim", "from pkg import run_token\nrun_token.redeem_claim()\n"),
    ("pkg/claims.py", "holds_claim", "from pkg.claims import holds_claim\n"),
    ("pkg/box_probe.py", "stored_capability", "from pkg import box_probe\nbox_probe.stored_capability()\n"),
]


def test_lost_definition_is_not_a_claim_provenance_reason() -> None:
    assert li.LOST_DEFINITION not in li.CLAIM_PROVENANCE_REASONS
