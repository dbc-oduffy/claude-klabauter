from __future__ import annotations

import json

import pytest

from coordinator_core.contract.cockpit_schema import emit_conformance_fixture as gen

_ENVELOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "version": "9.9.9",
    "additionalProperties": False,
    "required": ["schema_version", "handoffs", "plans"],
    "properties": {
        "schema_version": {"type": "string"},
        "handoffs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "review_verified"],
                "properties": {
                    "path": {"type": "string"},
                    "review_verified": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                },
            },
        },
        "plans": {"type": "array", "items": {"type": "object"}},
    },
}


def _committed(**overrides):
    base = {
        "schema_version": "2.7.0",
        "handoffs": [{"path": "state/handoffs/fixture.md"}],
        "plans": [],
        "contract_version": "2.7.0",
        "min_supported_contract_version": "2.7.0",
    }
    base.update(overrides)
    return base


def test_min_supported_is_preserved_never_derived():
    body = gen.build_fixture(_committed(), _ENVELOPE_SCHEMA)

    assert body["contract_version"] == "9.9.9", "contract_version must track the schema"
    assert body["min_supported_contract_version"] == "2.7.0", (
        "min_supported_contract_version is DoE-owned and must be carried across "
        "verbatim from the committed fixture"
    )
    assert (
        body["min_supported_contract_version"] != body["contract_version"]
    ), "min_supported was derived from contract_version -- forbidden, see module docstring"


def test_an_unusual_min_supported_is_still_carried_verbatim():
    body = gen.build_fixture(
        _committed(min_supported_contract_version="1.2.3"), _ENVELOPE_SCHEMA
    )
    assert body["min_supported_contract_version"] == "1.2.3"


def test_a_fixture_without_min_supported_is_refused_not_guessed():
    committed = _committed()
    del committed["min_supported_contract_version"]
    with pytest.raises(gen.FixtureGenerationError, match="min_supported"):
        gen.build_fixture(committed, _ENVELOPE_SCHEMA)


def test_a_schema_without_a_version_is_refused():
    """Refuses rather than falling back to this package's own CONTRACT_VERSION."""
    schema = dict(_ENVELOPE_SCHEMA)
    del schema["version"]
    with pytest.raises(gen.FixtureGenerationError, match="version"):
        gen.build_fixture(_committed(), schema)


def test_missing_required_fields_are_added_and_existing_values_kept():
    body = gen.build_fixture(_committed(), _ENVELOPE_SCHEMA)
    handoff = body["handoffs"][0]

    assert handoff["path"] == "state/handoffs/fixture.md", "existing value was rewritten"
    assert "review_verified" in handoff, "newly-required field was not added"


def test_fields_the_contract_removed_are_dropped():
    committed = _committed()
    committed["handoffs"][0]["file_attribution_id"] = "retired-field"
    body = gen.build_fixture(committed, _ENVELOPE_SCHEMA)

    assert "file_attribution_id" not in body["handoffs"][0]


def test_generated_envelope_validates_and_metadata_is_held_out():
    jsonschema = pytest.importorskip("jsonschema")
    body = gen.build_fixture(_committed(), _ENVELOPE_SCHEMA)

    envelope = gen.envelope_portion(body)
    assert set(envelope) == {"schema_version", "handoffs", "plans"}
    jsonschema.validate(envelope, _ENVELOPE_SCHEMA)


def test_regeneration_is_idempotent():
    once = gen.build_fixture(_committed(), _ENVELOPE_SCHEMA)
    twice = gen.build_fixture(json.loads(json.dumps(once)), _ENVELOPE_SCHEMA)
    assert gen.render(twice) == gen.render(once)


def test_render_matches_the_committed_byte_conventions():
    text = gen.render(gen.build_fixture(_committed(), _ENVELOPE_SCHEMA))
    assert text.endswith("\n"), "trailing newline"
    assert "\r" not in text, "LF, never CRLF"
    assert '\n  "' in text, "2-space indent"


def test_key_order_follows_the_schema_not_sorted():
    body = gen.build_fixture(_committed(), _ENVELOPE_SCHEMA)
    envelope_keys = [k for k in body if k not in gen._FIXTURE_METADATA_KEYS]
    assert envelope_keys == ["schema_version", "handoffs", "plans"]
    assert envelope_keys != sorted(envelope_keys), (
        "schema order happens to equal sorted order here -- this test can no longer "
        "tell the two apart; change the sample schema"
    )
