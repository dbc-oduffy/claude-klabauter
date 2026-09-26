
from __future__ import annotations

import sys
from pathlib import Path

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]
if str(_CLAUDE_KLABAUTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CLAUDE_KLABAUTER_ROOT))

from coordinator_core.frontmatter.schema_validate import describe, load_schemas, validate


_SCHEMA_NAMES = ["improvement-queue", "debt-backlog", "bug-backlog"]

# the test asserts BYTE-IDENTICAL membership so that future accidental additions
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

_EXPECTED_SYSTEM_BLOCK: dict = {
    "created_by_session": {"type": "string"},
    "created_by_agent": {"type": "string"},
    "linked_sessions": {"type": "array", "items": {"type": "string"}},
    "linked_commits": {"type": "array", "items": {"type": "string"}},
    "provenance_completeness": {"type": "string", "enum": ["complete", "unknown"]},
}


def _py_describe(schema_name: str) -> dict:
    try:
        return describe(schema_name)
    except ValueError as exc:
        raise AssertionError(f"schema_validate.describe({schema_name!r}) failed: {exc}") from exc


def _py_validate(schema_name: str, record: dict) -> tuple[bool, list[str]]:
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


def test_ac1_placement_guard() -> None:
    for name in _SCHEMA_NAMES:
        desc = _py_describe(name)

        optional_fields: list[str] = desc.get("optional") or []
        required_fields: list[str] = desc.get("required") or []

        assert "system" in optional_fields, (
            f"[{name}] 'system' not found in optional section.\n"
            f"optional fields: {optional_fields}"
        )

        assert "system" not in required_fields, (
            f"[{name}] 'system' was found in required section — must be optional only."
        )

        actual_required = frozenset(required_fields)
        expected_required = _REQUIRED_KEYS_BASELINE[name]
        assert actual_required == expected_required, (
            f"[{name}] required key set does not match baseline.\n"
            f"  Actual:   {sorted(actual_required)}\n"
            f"  Expected: {sorted(expected_required)}\n"
            "If a required field was legitimately added, update _REQUIRED_KEYS_BASELINE."
        )


def test_ac2_validate_with_system_block() -> None:
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
    for name in _SCHEMA_NAMES:
        flat_record = dict(_MINIMAL_VALID_RECORDS[name])
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


_SCHEMAS_DIR = _CLAUDE_KLABAUTER_ROOT / "coordinator_core" / "frontmatter" / "schemas"


def _py_load_system_blocks() -> dict[str, object]:
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
    py_blocks = _py_load_system_blocks()

    for name in _SCHEMA_NAMES:
        py_system = py_blocks.get(name)
        assert py_system is not None, (
            f"[{name}] properties.system.properties is None after load_schemas().\n"
            f"loaded schema names: {list(py_blocks.keys())}"
        )

        assert set(py_system.keys()) == set(_EXPECTED_SYSTEM_BLOCK.keys()), (
            f"[{name}] system block keys mismatch.\n"
            f"  Actual:   {sorted(py_system.keys())}\n"
            f"  Expected: {sorted(_EXPECTED_SYSTEM_BLOCK.keys())}"
        )

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
