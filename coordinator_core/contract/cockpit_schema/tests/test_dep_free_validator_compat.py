"""
test_dep_free_validator_compat — compatibility proof for the "one format" claim.

Pytest port of example-doctrine-repo `coordinator/cockpit-contract/test/dep-free-validator-compat.test.ts`.

Purpose: demonstrate that the dependency-free JSON-Schema-subset validator
(T4d's `coordinator_core.frontmatter.schema_validate`, ported from example-doctrine-repo
`coordinator/bin/lib/schema.js` — used by hooks, bash, CI) can READ and
validate records against a cockpit-emitted JSON Schema
(`schema/<entity>.schema.json`). This substantiates the ccos-1 architecture
claim that one on-disk FORMAT (JSON Schema files) is shared between the
dep-free validator and the cockpit-contract pydantic path.

Imports the T4d-ported validator IN-PROCESS (not shelling to node) per the
T4e-d build recipe's explicit instruction — T4e is sequenced after T4d so this
import is always available.

Spec backlink: docs/plans/2026-06-27-ccos-1-dual-context-validator.md § W3 Deliverable 2

KEYWORD GAP DOCUMENTATION (do NOT expand the subset here — see plan § W3):
The dep-free validator implements a draft-2020-12 SUBSET. Cockpit-emitted
schemas contain keywords the subset validator silently IGNORES: `pattern`
(zod's long ISO-8601 regex vs. the dep-free validator's simpler date-time
format check), `minimum`/`maximum` (safe-integer bounds — type:"integer" is
still checked, range is not), `$schema` (ignored — irrelevant for
validation), `minLength` (not enforced — an undocumented gap alongside the
above). The authoring source for cockpit entities remains pydantic; a record
that passes the dep-free validator but fails pydantic violates `pattern`,
integer range bounds, or `minLength` — acceptable for the hook/CI read path,
where the producer has already ensured the shape at emit time.
"""
from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import _validate_json_schema_node
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    load_fixture,
    load_schema,
    skip_no_fixtures,
    skip_no_schema,
)


def _validate_record(record: dict, schema: dict):
    """Twin of example-doctrine-repo `bin/lib/schema.js`'s `validateRecord(record, schema)` —
    ported minus the schema-version gate (irrelevant here: cockpit-emitted
    schemas carry no `x-schema-version`) and cross-field rules (keyed on
    `x-schema-name`, which cockpit-emitted schemas never carry — see
    `_apply_cross_field_rules`'s empty-list default). Phase 1 (shape
    validation via the JSON Schema subset) is the entirety of what this test
    exercises, matching `validateRecord`'s behavior on a cockpit schema
    1:1."""
    errors = _validate_json_schema_node(record, schema, schema, "")
    return {"ok": len(errors) == 0, "errors": errors}


@skip_no_fixtures
@skip_no_schema
def test_validate_record_accepts_valid_branch_fixture_against_cockpit_schema():
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    result = _validate_record(branch_fixture, branch_schema)
    assert result["ok"] is True, result["errors"]
    assert result["errors"] == []


@skip_no_fixtures
@skip_no_schema
def test_validate_record_rejects_branch_record_missing_required_field():
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    incomplete = dict(branch_fixture)
    del incomplete["provenance"]
    result = _validate_record(incomplete, branch_schema)
    assert result["ok"] is False
    field_names = [e["field"] for e in result["errors"]]
    assert "provenance" in field_names


@skip_no_fixtures
@skip_no_schema
def test_validate_record_accepts_any_nonempty_owner_string_no_closed_enum():
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    arbitrary_owner = {**branch_fixture, "owner": "not-a-real-org"}
    result = _validate_record(arbitrary_owner, branch_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_validate_record_accepts_empty_owner_minlength_not_enforced_gap():
    """The dep-free validator's JSON Schema subset does NOT implement
    minLength enforcement (an undocumented gap alongside pattern/minimum/
    maximum). The pydantic authoring source enforces min_length=1 at
    emission time — an empty owner never reaches this read-path validator
    in practice. This documents dep-free-validator behavior rather than
    asserting a rejection the dep-free validator cannot structurally
    produce."""
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    empty_owner = {**branch_fixture, "owner": ""}
    result = _validate_record(empty_owner, branch_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_validate_record_rejects_extra_unknown_field_additional_properties_false():
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    extra_field = {**branch_fixture, "unknown_extra_field": "oops"}
    result = _validate_record(extra_field, branch_schema)
    assert result["ok"] is False
    extra = [e for e in result["errors"] if "unknown_extra_field" in e["field"]]
    assert extra


@skip_no_fixtures
@skip_no_schema
def test_validate_record_accepts_branch_record_with_nullable_fields_set_to_null():
    branch_schema = load_schema("branch")
    branch_fixture = load_fixture("branch")
    nullable_fields = {
        **branch_fixture,
        "merge_base_sha": None,
        "ahead_by": None,
        "behind_by": None,
        "machine_hint": None,
        "date_hint": None,
    }
    result = _validate_record(nullable_fields, branch_schema)
    assert result["ok"] is True, result["errors"]


def test_validate_record_resolves_defs_ref_schema():
    """Schema uses $defs to define a shared type; the top-level property
    references it via $ref."""
    schema_with_defs = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["id", "label"],
        "additionalProperties": False,
        "$defs": {"LabelDef": {"type": "string"}},
        "properties": {
            "id": {"type": "string"},
            "label": {"$ref": "#/$defs/LabelDef"},
        },
    }

    valid_record = {"id": "ref-001", "label": "hello"}
    result = _validate_record(valid_record, schema_with_defs)
    assert result["ok"] is True, result["errors"]

    invalid_record = {"id": "ref-002", "label": 42}
    bad_result = _validate_record(invalid_record, schema_with_defs)
    assert bad_result["ok"] is False
    label_error = [e for e in bad_result["errors"] if "label" in e["field"]]
    assert label_error


def test_validate_record_handles_nullable_required_field_anyof_null():
    """Schema with a required field that is nullable (anyOf: [type, null]) —
    mirrors the pattern used by branch.schema.json fields like merge_base_sha."""
    schema_with_nullable_required = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["id", "commit_sha"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "commit_sha": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }

    null_record = {"id": "nullable-001", "commit_sha": None}
    null_result = _validate_record(null_record, schema_with_nullable_required)
    assert null_result["ok"] is True, null_result["errors"]

    string_record = {"id": "nullable-002", "commit_sha": "abc123"}
    string_result = _validate_record(string_record, schema_with_nullable_required)
    assert string_result["ok"] is True

    missing_record = {"id": "nullable-003"}
    missing_result = _validate_record(missing_record, schema_with_nullable_required)
    assert missing_result["ok"] is False
    missing_error = [e for e in missing_result["errors"] if "commit_sha" in e["field"]]
    assert missing_error


# C3 — JSON Schema side: handoff-summary.schema.json accepts/rejects lineage
# fields correctly. The dep-free validator must accept records both WITH and
# WITHOUT the new optional fields, and must reject an extra unknown field
# (proving additionalProperties:false is still intact).
# Spec backlink: docs/plans/2026-06-29-handoff-lineage-dag-fan-in-fan-out.md § C3 (AC6 half).


@skip_no_fixtures
@skip_no_schema
def test_handoff_summary_baseline_fixture_without_lineage_fields_validates():
    handoff_schema = load_schema("handoff-summary")
    handoff_fixture = load_fixture("handoff-summary")
    result = _validate_record(handoff_fixture, handoff_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_handoff_summary_record_with_forked_from_string_validates():
    handoff_schema = load_schema("handoff-summary")
    handoff_fixture = load_fixture("handoff-summary")
    record = {**handoff_fixture, "forked_from": "abc1234def5678"}
    result = _validate_record(record, handoff_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_handoff_summary_record_with_additional_predecessors_array_validates():
    handoff_schema = load_schema("handoff-summary")
    handoff_fixture = load_fixture("handoff-summary")
    record = {**handoff_fixture, "additional_predecessors": ["sha-a", "sha-b"]}
    result = _validate_record(record, handoff_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_handoff_summary_record_with_both_lineage_fields_validates():
    handoff_schema = load_schema("handoff-summary")
    handoff_fixture = load_fixture("handoff-summary")
    record = {
        **handoff_fixture,
        "additional_predecessors": ["sha-parent-1"],
        "forked_from": "sha-fork-origin",
    }
    result = _validate_record(record, handoff_schema)
    assert result["ok"] is True, result["errors"]


@skip_no_fixtures
@skip_no_schema
def test_handoff_summary_additional_properties_false_still_intact():
    handoff_schema = load_schema("handoff-summary")
    handoff_fixture = load_fixture("handoff-summary")
    record = {**handoff_fixture, "completely_unknown_field_xyz": "oops"}
    result = _validate_record(record, handoff_schema)
    assert result["ok"] is False
    extra = [e for e in result["errors"] if "completely_unknown_field_xyz" in e["field"]]
    assert extra
