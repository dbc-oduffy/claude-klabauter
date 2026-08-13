"""
test_emission_scope — parse/reject tests for ScopedEmission (v2.14.0).

Pytest port of coordinator-claude `coordinator/cockpit-contract/test/emission-scope.test.ts`.
Asserts each of the three scope cases parses with its required envelope, and
that a record missing its scope-required envelope key, or carrying the wrong
envelope for its declared scope, is rejected.

Spec backlink: DECISIONS.md § D25
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.emission_scope import ScopedEmission
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "coordinator_artifact",
    "repo": "dbc-oduffy/coordinator-claude",
    "ref": None,
    "path": "state/example.md",
    "observed_at": "2026-07-13T12:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

CONTENT_HASH = {
    "algo": "sha256",
    "hex": "a" * 64,
    "source_path": "state/example.md",
}

AUTHORITY = {
    "emitter": "machine-b",
    "emitter_repo": "dbc-oduffy/coordinator-claude",
}


def test_valid_per_repo_parses():
    zod_parse(ScopedEmission, {"scope": "per-repo", "repo": "dbc-oduffy/coordinator-claude", "provenance": PROV})


def test_valid_fleet_derived_parses():
    zod_parse(ScopedEmission, {"scope": "fleet-derived", "content_hash": CONTENT_HASH})


def test_valid_fleet_authored_parses():
    zod_parse(ScopedEmission, {"scope": "fleet-authored", "authority": AUTHORITY})


def test_rejects_per_repo_missing_repo():
    assert not zod_safe_parse_ok(
        ScopedEmission, {"scope": "per-repo", "provenance": PROV}
    )


def test_rejects_per_repo_missing_provenance():
    assert not zod_safe_parse_ok(
        ScopedEmission, {"scope": "per-repo", "repo": "dbc-oduffy/coordinator-claude"}
    )


def test_rejects_fleet_derived_missing_content_hash():
    assert not zod_safe_parse_ok(ScopedEmission, {"scope": "fleet-derived"})


def test_rejects_fleet_authored_missing_authority():
    assert not zod_safe_parse_ok(ScopedEmission, {"scope": "fleet-authored"})


def test_rejects_unknown_scope_value():
    assert not zod_safe_parse_ok(
        ScopedEmission,
        {"scope": "global", "repo": "dbc-oduffy/coordinator-claude", "provenance": PROV},
    )


def test_rejects_per_repo_carrying_content_hash_instead_of_provenance():
    assert not zod_safe_parse_ok(
        ScopedEmission, {"scope": "per-repo", "content_hash": CONTENT_HASH}
    )


def test_rejects_fleet_derived_carrying_authority_instead_of_content_hash():
    assert not zod_safe_parse_ok(
        ScopedEmission, {"scope": "fleet-derived", "authority": AUTHORITY}
    )


def test_rejects_fleet_authored_carrying_content_hash_instead_of_authority():
    assert not zod_safe_parse_ok(
        ScopedEmission, {"scope": "fleet-authored", "content_hash": CONTENT_HASH}
    )


def test_rejects_per_repo_record_with_extra_content_hash_key():
    """additionalProperties:false strictness proof — a well-formed envelope
    plus an extra key from another variant must reject."""
    assert not zod_safe_parse_ok(
        ScopedEmission,
        {
            "scope": "per-repo",
            "repo": "dbc-oduffy/coordinator-claude",
            "provenance": PROV,
            "content_hash": CONTENT_HASH,
        },
    )
