"""
Tests for coordinator_core.frontmatter.schema_validate's `x-bump-adds` guard.

`x-bump-adds` is a structured sibling to the prose `x-bump-note`: a list of
property paths / enum values a bump introduces. This guard asserts that
every entry actually resolves in the schema it is attached to — a bump can
no longer claim an addition its committed bytes do not contain.

Exercised entirely against SYNTHETIC in-test schema dicts — never a real
vendored schema file. `x-bump-adds` must not appear in any file under
coordinator_core/frontmatter/schemas/: DoE's shape-parity gate strips only a
named allowlist of authoring annotations (`_AUTHORING_ANNOTATION_KEYWORDS`),
does not yet include `x-bump-adds`, and the key becoming shape-visible on a
real vendored schema reds 15-17 vendored pairs at once. See
state/improvement-queue/2026-08-10-vendored-schemas-have-no-machine-checkab-405eb7f5c628.yaml.

Coverage targets:
  - key absent → inert (no errors) — every claude-klabauter schema's current state
  - x-bump-adds: [] → valid, explicit "this bump adds nothing" claim
  - non-empty list, every entry resolves → valid
  - non-empty list, one entry does not resolve → one error, field-scoped
  - dotted-path entries into nested `properties`
  - enum-member entries via "path:value" syntax
  - non-list `x-bump-adds` value → a shape error, not a crash
"""
from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import (
    check_bump_adds_self_consistency,
)


def test_absent_key_is_inert():
    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
    }
    assert check_bump_adds_self_consistency(schema) == []


def test_empty_list_is_a_valid_explicit_no_additions_claim():
    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "x-bump-adds": [],
    }
    assert check_bump_adds_self_consistency(schema) == []


def test_non_empty_list_every_entry_resolves():
    schema = {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"bar": {"type": "string"}},
            },
        },
        "x-bump-adds": ["properties.foo", "properties.nested.properties.bar"],
    }
    assert check_bump_adds_self_consistency(schema) == []


def test_entry_that_does_not_resolve_fails():
    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "x-bump-adds": ["properties.foo", "properties.does_not_exist"],
    }
    errors = check_bump_adds_self_consistency(schema)
    assert len(errors) == 1
    assert errors[0]["field"] == "x-bump-adds"
    assert "does_not_exist" in errors[0]["error"]


def test_enum_member_entry_resolves_via_colon_syntax():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["alpha", "beta"]},
        },
        "x-bump-adds": ["properties.kind:beta"],
    }
    assert check_bump_adds_self_consistency(schema) == []


def test_enum_member_entry_that_does_not_resolve_fails():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["alpha", "beta"]},
        },
        "x-bump-adds": ["properties.kind:gamma"],
    }
    errors = check_bump_adds_self_consistency(schema)
    assert len(errors) == 1
    assert "gamma" in errors[0]["error"]


def test_non_list_value_is_a_shape_error_not_a_crash():
    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "x-bump-adds": "properties.foo",
    }
    errors = check_bump_adds_self_consistency(schema)
    assert len(errors) == 1
    assert errors[0]["field"] == "x-bump-adds"
