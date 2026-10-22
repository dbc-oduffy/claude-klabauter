"""`closed` is terminal exactly where `abandoned` is, and never flippable."""
from __future__ import annotations

import pytest

from coordinator_core import lifecycle_constants as lc
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.emit import deliverable_status as ds
from coordinator_core.roadmap import plan_gate
from coordinator_core.updatedocs import plan_prune

_SETS = {
    "PLAN_ARCHIVABLE_STATUS": lc.PLAN_ARCHIVABLE_STATUS,
    "PLAN_ORPHAN_TERMINAL_STATUS": lc.PLAN_ORPHAN_TERMINAL_STATUS,
    "_FROZEN_STATUSES": pst._FROZEN_STATUSES,
    "TERMINAL_PLAN_STATUSES": plan_prune.TERMINAL_PLAN_STATUSES,
    "_STATUS_LIFECYCLE_ORDER": frozenset(plan_gate._STATUS_LIFECYCLE_ORDER),
}


@pytest.mark.parametrize("name", sorted(_SETS))
def test_closed_in_set(name):
    assert "closed" in _SETS[name]


@pytest.mark.parametrize("name", sorted(_SETS))
def test_closed_membership_equals_abandoned(name):
    assert ("closed" in _SETS[name]) == ("abandoned" in _SETS[name])


def test_plan_terminal_alias_carries_closed():
    assert "closed" in lc.PLAN_TERMINAL_STATUS


def test_lifecycle_order_closed_follows_abandoned():
    order = plan_gate._STATUS_LIFECYCLE_ORDER
    assert order.index("closed") == order.index("abandoned") + 1


def test_plan_phase_closed_is_abandoned():
    assert ds._plan_phase({"status": "closed"}) == "abandoned"


def test_closed_not_flippable():
    assert "closed" not in pst._FLIPPABLE_STATUSES


def test_spec_skip_untouched():
    assert "closed" not in lc.SPEC_SKIP_STATUSES
