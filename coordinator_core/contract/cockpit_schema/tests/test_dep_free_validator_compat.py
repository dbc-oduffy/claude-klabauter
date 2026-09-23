"""
test_dep_free_validator_compat — compatibility proof for the "one format" claim.

Pytest port of DoE `coordinator/cockpit-contract/test/dep-free-validator-compat.test.ts`.

Purpose: demonstrate that the dependency-free JSON-Schema-subset validator
(T4d's `coordinator_core.frontmatter.schema_validate`, ported from DoE
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


def _validate_record(record: dict, schema: dict):
    """Twin of DoE `bin/lib/schema.js`'s `validateRecord(record, schema)` —
    ported minus the schema-version gate (irrelevant here: cockpit-emitted
    schemas carry no `x-schema-version`) and cross-field rules (keyed on
    `x-schema-name`, which cockpit-emitted schemas never carry — see
    `_apply_cross_field_rules`'s empty-list default). Phase 1 (shape
    validation via the JSON Schema subset) is the entirety of what this test
    exercises, matching `validateRecord`'s behavior on a cockpit schema
    1:1."""
    errors = _validate_json_schema_node(record, schema, schema, "")
    return {"ok": len(errors) == 0, "errors": errors}


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
