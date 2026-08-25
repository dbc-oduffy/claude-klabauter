"""
test_handoff_kind_baton_widen — union non-narrowing guard for the D1
baton-kind vocabulary widen (C8a, critical-path chunk).

`HandoffKind` (entities/summaries.py) and its emitted wire twin
(DoE-claude `coordinator/cockpit-contract/schema/handoff-summary.schema.json`
`properties.kind.enum`) must both accept the three D1 rename targets
("roadmap-baton", "roadmap-seed", "goal-seed") AND still carry every
pre-existing value, including the three tokens those targets retire on the
live surface ("spinoff-roadmap", "spinoff-roadmap-creator", "spinoff-goal")
and the unrelated grandfathered "spike-result". The cockpit `handoffs` array
carries archived records as well as live ones, so narrowing HandoffKind ever
would silently drop archived records from the emission instead of failing
loudly — this test asserts the widen is a pure union (superset), not a
substitution, on both the Python Literal and the generated JSON Schema.

Spec backlink: DoE-claude:pln-baton-kind-vocabulary-one-axis-d1ce8f § D1/C8a
"""
from __future__ import annotations

import typing

from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffKind
from coordinator_core.contract.cockpit_schema.tests.conftest import load_schema, skip_no_schema

_PRE_EXISTING_VALUES = frozenset(
    {
        "session-handoff",
        "spinoff",
        "spinoff-roadmap",
        "recovery",
        "spinoff-goal",
        "spinoff-roadmap-creator",
        "spike-result",
    }
)
_NEW_D1_VALUES = frozenset({"roadmap-baton", "roadmap-seed", "goal-seed"})
_EXPECTED_FULL_SET = _PRE_EXISTING_VALUES | _NEW_D1_VALUES


def test_handoff_kind_literal_widened_not_narrowed():
    actual = frozenset(typing.get_args(HandoffKind))

    missing_pre_existing = _PRE_EXISTING_VALUES - actual
    assert not missing_pre_existing, (
        "HandoffKind dropped pre-existing value(s) — this must stay additive-only "
        f"(archived records may carry any historical kind): {sorted(missing_pre_existing)}"
    )

    missing_new = _NEW_D1_VALUES - actual
    assert not missing_new, (
        f"HandoffKind is missing D1 baton-kind rename target(s): {sorted(missing_new)}"
    )

    assert actual == _EXPECTED_FULL_SET, (
        "HandoffKind is not the expected pure union of pre-existing + new D1 values — "
        f"unexpected extra/missing members. actual={sorted(actual)} "
        f"expected={sorted(_EXPECTED_FULL_SET)}"
    )


@skip_no_schema
def test_handoff_summary_wire_schema_kind_enum_widened_not_narrowed():
    schema = load_schema("handoff-summary")
    actual = frozenset(schema["properties"]["kind"]["enum"])

    missing_pre_existing = _PRE_EXISTING_VALUES - actual
    assert not missing_pre_existing, (
        "handoff-summary.schema.json kind enum dropped pre-existing value(s) — "
        f"this must stay additive-only: {sorted(missing_pre_existing)}"
    )

    missing_new = _NEW_D1_VALUES - actual
    assert not missing_new, (
        "handoff-summary.schema.json kind enum is missing D1 baton-kind rename "
        f"target(s): {sorted(missing_new)}"
    )

    assert actual == _EXPECTED_FULL_SET, (
        "handoff-summary.schema.json kind enum is not the expected pure union of "
        f"pre-existing + new D1 values. actual={sorted(actual)} "
        f"expected={sorted(_EXPECTED_FULL_SET)}"
    )
