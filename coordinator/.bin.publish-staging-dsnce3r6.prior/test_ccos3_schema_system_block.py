"""
test_ccos3_schema_system_block.py — tests for AC1/AC2/AC3 of the ccos-3 system: provenance block.

Spec backlink: state/handoffs/2026-06-27_095003_roadmap-ccos-3.md § Specification

Retiring-ruling backlink: 2026-08-02 fast-tier stale-test triage
(tasks/mise-verify/triage-C-cockpit.md § test_ccos3_schema_system_block.py). This
file used to shell out to `node` for `coordinator/bin/lib/schema.js` and
`coordinator/bin/schema-cli.js`; both were deleted by commit 480ad8f86 (the
D1 retirement of the Node schema oracle and `bin/lib/*.js`, fleet-reachability-gated)
when that oracle was retired in favor of the native Python one
(coordinator_core/frontmatter/schema_validate.py, schema_cli.py). This test
file was a straggler D1 did not port alongside its two `.js` dependencies —
ported here to call schema_validate.describe()/validate()/load_schemas()
in-process instead, mirroring the same conversion D1 already made for
test_schema_validate.py and test_emit_artifact_shape_contract.py. Per
CLAUDE.md § Runtime conventions (no Node runtime is required for this repo's
own work), this was the one surviving caller that still required one.

Tests:
  AC1 (placement_guard): 'system' is in optional, NOT in required, for all three schemas.
      Required key sets are byte-identical to the hardcoded baselines below.
  AC2 (validate): records with and without a system block both pass --validate.
  AC3 (system_block_golden): the vendored JSON-Schema `system` property (as loaded
      by schema_validate.load_schemas()) has the expected key structure and
      provenance_completeness enum values.

Runnable via: python3 -m pytest bin/test_ccos3_schema_system_block.py
Exit 0 = all tests pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]
if str(_CLAUDE_KLABAUTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CLAUDE_KLABAUTER_ROOT))

from coordinator_core.frontmatter.schema_validate import describe, load_schemas, validate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All three schemas under test.
_SCHEMA_NAMES = ["improvement-queue", "debt-backlog", "bug-backlog"]

# Hardcoded required-key baselines — sourced from reading the schema YAML files
# on disk as of the ccos-3 authoring pass. These are the authoritative sets;
# the test asserts BYTE-IDENTICAL membership so that future accidental additions
# to required: are caught immediately.
_REQUIRED_KEYS_BASELINE: dict[str, frozenset[str]] = {
    "improvement-queue": frozenset({
        "created", "title", "body", "status", "surface",
        "proposed_action", "from_repo", "change_kind",
    }),
    "debt-backlog": frozenset({
        "created", "title", "body", "status", "source",
        "risk", "proposed_action",
    }),
    "bug-backlog": frozenset({
        "created", "title", "body", "status", "surface", "severity",
    }),
}

# Minimal valid required fields for each schema (used in AC2 validate tests).
_MINIMAL_VALID_RECORDS: dict[str, dict] = {
    "improvement-queue": {
        "created": "2026-06-27",
        "title": "Test improvement",
        "body": "Some body text",
        "status": "open",
        "surface": "bin/some-script.py",
        "proposed_action": "Refactor X",
        "from_repo": "claude-central",
        "change_kind": "script-edit",
    },
    "debt-backlog": {
        "created": "2026-06-27",
        "title": "Test debt",
        "body": "Some body text",
        "status": "open",
        "source": "code-review",
        "risk": "medium",
        "proposed_action": "Refactor Y",
    },
    "bug-backlog": {
        "created": "2026-06-27",
        "title": "Test bug",
        "body": "Some body text",
        "status": "open",
        "surface": "bin/some-script.py",
        "severity": "P2",
    },
}

# Expected system-block structure (same across all three schemas). Shape is
# the JSON-Schema `properties.system.properties` nesting the vendored
# .schema.json files actually declare (see e.g.
# coordinator_core/frontmatter/schemas/bug-backlog.schema.json's "system" key)
# — the old node-era golden used the retired YAML-dialect shorthand
# ("string"/"list-of-string"/{"type": "enum", ...}); JSON Schema has no such
# shorthand, so each leaf is asserted via its own "type"/"items"/"enum" keys.
_EXPECTED_SYSTEM_BLOCK: dict = {
    "created_by_session": {"type": "string"},
    "created_by_agent": {"type": "string"},
    "linked_sessions": {"type": "array", "items": {"type": "string"}},
    "linked_commits": {"type": "array", "items": {"type": "string"}},
    "provenance_completeness": {"type": "string", "enum": ["complete", "unknown"]},
}

# ---------------------------------------------------------------------------
# Native schema_validate helpers (in-process — no node subprocess).
# ---------------------------------------------------------------------------

def _py_describe(schema_name: str) -> dict:
    """Call schema_validate.describe(schema_name); re-raise as AssertionError.

    Returns { required: [...], optional: [...], enums: {...}, applies_to: ... },
    the same shape schema-cli.js's --describe used to print.
    """
    try:
        return describe(schema_name)
    except ValueError as exc:
        raise AssertionError(f"schema_validate.describe({schema_name!r}) failed: {exc}") from exc


def _py_validate(schema_name: str, record: dict) -> tuple[bool, list[str]]:
    """Call schema_validate.validate(schema_name, record); flatten to (ok, errors).

    Mirrors schema_cli.py's own _cmd_validate flattening (field: error strings),
    the same envelope schema-cli.js's --validate used to print.
    """
    try:
        result = validate(schema_name, record)
    except ValueError as exc:
        raise AssertionError(f"schema_validate.validate({schema_name!r}, ...) failed: {exc}") from exc

    ok: bool = result.get("ok") is True
    if ok:
        return True, []
    errors: list[str] = []
    for e in result.get("errors") or []:
        field = e.get("field") if isinstance(e, dict) else None
        error = e.get("error") if isinstance(e, dict) else None
        field_part = f"{field}: " if field else ""
        errors.append(f"{field_part}{error or ''}")
    return False, errors


# ---------------------------------------------------------------------------
# AC1: placement guard — system in optional, NOT in required
# ---------------------------------------------------------------------------

def test_ac1_placement_guard() -> None:
    """AC1: 'system' in optional and NOT in required, for all three schemas.

    Also asserts that the required key set is byte-identical to the hardcoded
    baseline so that accidental required: additions are caught immediately.
    """
    for name in _SCHEMA_NAMES:
        desc = _py_describe(name)

        optional_fields: list[str] = desc.get("optional") or []
        required_fields: list[str] = desc.get("required") or []

        # 'system' must be in optional.
        assert "system" in optional_fields, (
            f"[{name}] 'system' not found in optional section.\n"
            f"optional fields: {optional_fields}"
        )

        # 'system' must NOT be in required.
        assert "system" not in required_fields, (
            f"[{name}] 'system' was found in required section — must be optional only."
        )

        # Required key set must match the baseline exactly.
        actual_required = frozenset(required_fields)
        expected_required = _REQUIRED_KEYS_BASELINE[name]
        assert actual_required == expected_required, (
            f"[{name}] required key set does not match baseline.\n"
            f"  Actual:   {sorted(actual_required)}\n"
            f"  Expected: {sorted(expected_required)}\n"
            "If a required field was legitimately added, update _REQUIRED_KEYS_BASELINE."
        )


# ---------------------------------------------------------------------------
# AC2: validate — records with and without system block both pass
# ---------------------------------------------------------------------------

def test_ac2_validate_with_system_block() -> None:
    """AC2a: a record with a populated system block passes --validate."""
    for name in _SCHEMA_NAMES:
        record_with_system = dict(_MINIMAL_VALID_RECORDS[name])
        record_with_system["system"] = {
            "created_by_session": "ses-abc123",
            "created_by_agent": "executor",
            "linked_sessions": ["ses-abc123"],
            "linked_commits": ["abc1234"],
            "provenance_completeness": "complete",
        }

        ok, errors = _py_validate(name, record_with_system)
        assert ok, (
            f"[{name}] --validate returned false for a record WITH system block.\n"
            f"errors: {errors!r}"
        )
        assert errors == [], (
            f"[{name}] --validate returned errors for a valid record with system block.\n"
            f"errors: {errors!r}"
        )


def test_ac2_validate_without_system_block() -> None:
    """AC2b: a flat historical record (no system block) also passes --validate.

    Confirms backward-compatibility: pre-ccos3 records that have no 'system'
    key at all continue to validate without error.
    """
    for name in _SCHEMA_NAMES:
        flat_record = dict(_MINIMAL_VALID_RECORDS[name])
        # Explicitly confirm system is absent
        assert "system" not in flat_record

        ok, errors = _py_validate(name, flat_record)
        assert ok, (
            f"[{name}] --validate returned false for a flat historical record (no system block).\n"
            f"errors: {errors!r}"
        )
        assert errors == [], (
            f"[{name}] --validate returned errors for a flat historical record.\n"
            f"errors: {errors!r}"
        )


# ---------------------------------------------------------------------------
# AC3: system_block_golden — the vendored JSON Schema parses the system block
# as expected (single-parser golden; the node parser no longer exists).
# ---------------------------------------------------------------------------

_SCHEMAS_DIR = _CLAUDE_KLABAUTER_ROOT / "coordinator_core" / "frontmatter" / "schemas"


def _py_load_system_blocks() -> dict[str, object]:
    """Call schema_validate.load_schemas() and extract each schema's
    properties.system.properties block (the JSON-Schema-native equivalent of
    the retired schema.js loadSchemas().optional.system lookup).

    Returns a dict mapping schema_name -> system-block properties dict, or
    None if the schema has no "system" property.
    """
    schemas = load_schemas(_SCHEMAS_DIR)
    result: dict[str, object] = {}
    for name in _SCHEMA_NAMES:
        schema = schemas.get(name)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        system = properties.get("system") if isinstance(properties, dict) else None
        system_props = system.get("properties") if isinstance(system, dict) else None
        result[name] = system_props if isinstance(system_props, dict) else None
    return result


def test_ac3_system_block_golden() -> None:
    """AC3: the vendored JSON Schema parses the system block with the expected
    structure (single-parser golden). Retiring-ruling backlink: see module
    docstring — the node-era dual-parser parity assertion is void since D1
    retired the sole node parser; this asserts the Python-native
    load_schemas() parse of properties.system produces the correct key
    nesting and provenance_completeness enum values.

    Negative-spec: does NOT compare against a second (e.g. node) parser —
    only one parser exists now.
    """
    py_blocks = _py_load_system_blocks()

    for name in _SCHEMA_NAMES:
        py_system = py_blocks.get(name)
        assert py_system is not None, (
            f"[{name}] properties.system.properties is None after load_schemas().\n"
            f"loaded schema names: {list(py_blocks.keys())}"
        )

        # Assert system block key set matches expected.
        assert set(py_system.keys()) == set(_EXPECTED_SYSTEM_BLOCK.keys()), (
            f"[{name}] system block keys mismatch.\n"
            f"  Actual:   {sorted(py_system.keys())}\n"
            f"  Expected: {sorted(_EXPECTED_SYSTEM_BLOCK.keys())}"
        )

        # Assert each leaf's declared shape matches the expected JSON-Schema shape.
        for field, expected_spec in _EXPECTED_SYSTEM_BLOCK.items():
            actual_spec = py_system.get(field)
            assert isinstance(actual_spec, dict), (
                f"[{name}] system.{field} is not a dict: {actual_spec!r}"
            )
            for key, expected_value in expected_spec.items():
                assert actual_spec.get(key) == expected_value, (
                    f"[{name}] system.{field}.{key} mismatch.\n"
                    f"  Actual:   {actual_spec.get(key)!r}\n"
                    f"  Expected: {expected_value!r}"
                )
