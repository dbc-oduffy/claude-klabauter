"""C9: the human_* contract fields are authored, optional, and nullable.

Purpose: pins half (a) of C9 — `human_owner`/`human_assignee`/`human_claimant` exist on
the relevant cockpit-schema entities as genuinely OPTIONAL (absence-allowed) AND nullable
fields, the same `x-zod-nullable-optional` shape as `HandoffSummary.additional_predecessors`
et al. — never a required-with-null (D9) field, and never a value change to the pre-existing
`owner` key. Also pins the MINOR `CONTRACT_VERSION` bump this addition rides.

This module does NOT exercise activation (whether an emit section actually populates these
keys) — that is `ops/emit/tests/test_human_axis_emission.py`'s job. This module is schema-shape
only: the fields exist, they validate correctly with and without a value, and they do not widen
what `owner`/pre-existing keys carry.

Spec backlink: docs/plans/2026-08-19-the-tracker-names-an-owner.md § C9
"""

from __future__ import annotations

from coordinator_core.contract.cockpit_schema import CONTRACT_VERSION
from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffSummary
from coordinator_core.contract.cockpit_schema.entities.tracker_summary import TrackerSummary

_BASE_PROVENANCE = {
    "source_kind": "local_fs",
    "derivation": "parsed",
    "observed_at": "2026-08-20T00:00:00Z",
    "ref": None,
    "repo": "acme/widgets",
    "path": "docs/thing.md",
    "entity_anchor": None,
}


def _base_handoff_kwargs() -> dict:
    """Minimal required-field set for a valid HandoffSummary, human_* omitted."""
    return dict(
        repo="acme/widgets",
        coordinator_root_path=".",
        title="t",
        created="2026-08-20",
        status="open",
        kind="session-handoff",
        baton_class=None,
        deployment_state="continued",
        workstream="",
        predecessor="none",
        scope=[],
        claimed_by=None,
        claimed_at=None,
        continued_into=None,
        closed_reason=None,
        shipped_in=None,
        picked_up_by=None,
        acceptance_criteria=None,
        provenance=_BASE_PROVENANCE,
        deliverable_id=None,
        plan_id=None,
        initiative=None,
        caption=None,
        status_reason=None,
        owner=None,
        last_meaningful_activity=None,
        workstream_type=None,
        shipped_sha=None,
        deliverable_status=None,
        origin_session=None,
        origin_handoff=None,
        origin_plan_id=None,
        origin_goal_id=None,
        handoff_id="hnd-t-abc123",
        handoff_id_derivation="derived",
        pm_priority=None,
        pm_priority_origin=None,
        pm_priority_source_id=None,
        suggested_priority=None,
        producer=None,
    )


def test_handoff_summary_human_fields_absent_by_default():
    """Omitting human_assignee/human_claimant entirely still validates (OPTIONAL, not
    required-with-null) and model_dump() materializes them as null — the emit section's
    own job (not this test's) is to strip that null back out while the switch is off."""
    model = HandoffSummary(**_base_handoff_kwargs())
    dumped = model.model_dump()
    assert dumped["human_assignee"] is None
    assert dumped["human_claimant"] is None


def test_handoff_summary_human_fields_accept_a_value():
    kwargs = _base_handoff_kwargs()
    kwargs["human_assignee"] = "abc123def"
    kwargs["human_claimant"] = "abc123def"
    model = HandoffSummary(**kwargs)
    dumped = model.model_dump()
    assert dumped["human_assignee"] == "abc123def"
    assert dumped["human_claimant"] == "abc123def"


def test_handoff_summary_human_fields_never_widen_owner():
    """`owner` keeps its pre-existing type/semantics — adding human_* is additive, not
    a repurposing of the existing key."""
    kwargs = _base_handoff_kwargs()
    kwargs["human_assignee"] = "abc123def"
    model = HandoffSummary(**kwargs)
    assert model.owner is None
    assert HandoffSummary.model_fields["owner"].annotation == (str | None)


def test_tracker_summary_human_owner_absent_by_default():
    model = TrackerSummary(
        repo="acme/widgets",
        coordinator_root_path=".",
        path="docs/project-tracker.md",
        title="t",
        created="2026-08-20",
        status="active",
        provenance=_BASE_PROVENANCE,
        owner=None,
        items=None,
    )
    dumped = model.model_dump()
    assert dumped["human_owner"] is None
    assert model.owner is None


def test_tracker_summary_human_owner_accepts_a_value():
    model = TrackerSummary(
        repo="acme/widgets",
        coordinator_root_path=".",
        path="docs/project-tracker.md",
        title="t",
        created="2026-08-20",
        status="active",
        provenance=_BASE_PROVENANCE,
        owner="platform-team",
        items=None,
        human_owner="abc123def",
    )
    dumped = model.model_dump()
    assert dumped["human_owner"] == "abc123def"
    # `owner` is untouched by the new key — still the pre-existing free-text value.
    assert dumped["owner"] == "platform-team"


def test_human_fields_are_optional_nullable_not_required_with_null():
    """The x-zod-nullable-optional marker (json_schema_extra) is present on all three
    fields, distinguishing them from D9 required-with-null fields on the same entities
    (e.g. `owner`, which carries no such marker and is a plain required key)."""
    for field_name in ("human_assignee", "human_claimant"):
        field = HandoffSummary.model_fields[field_name]
        assert field.json_schema_extra == {"x-zod-nullable-optional": True}
        assert field.default is None

    tracker_field = TrackerSummary.model_fields["human_owner"]
    assert tracker_field.json_schema_extra == {"x-zod-nullable-optional": True}
    assert tracker_field.default is None

    owner_field = HandoffSummary.model_fields["owner"]
    assert owner_field.json_schema_extra != {"x-zod-nullable-optional": True}


def test_contract_version_bumped_minor_for_additive_human_axis():
    """3.12.0 -> 3.13.0: additive-only MINOR bump, same class as every prior additive
    row in emit_schema.py's CONTRACT_VERSION changelog comment."""
    major, minor, patch = (int(p) for p in CONTRACT_VERSION.split("."))
    assert (major, minor) >= (3, 13)
