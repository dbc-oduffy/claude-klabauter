"""Tests for schema_validate.validate_frontmatter_obj() — the schema-OBJECT-in
seam requested by DoE's validate-frontmatter-schema.js node-hook port (cross-repo
memo 2026-07-22, claude-central-em). Distinct from test_schema_validate.py's
coverage of validate()/validate_frontmatter(), which are name-keyed against
Claude-klabauter's own vendored _SCHEMAS_DIR — these tests construct schema objects
inline (never touching disk) to prove the function validates a foreign
caller-supplied schema corpus without shadowing through claude-klabauter's vendored set,
and that it never raises regardless of malformed input.
"""

from __future__ import annotations

import datetime

from coordinator_core.frontmatter.schema_validate import (
    SchemaVersionError,
    validate_frontmatter_obj,
)

# A minimal JSON-Schema-backed schema object, stamped exactly as load_schemas()
# would stamp a .schema.json file — constructed inline so no schema directory
# is ever read by this test module.
_JSON_SCHEMA_OBJ = {
    '_isJsonSchema': True,
    'x-schema-name': 'fixture-json-schema',
    'type': 'object',
    'required': ['title', 'status'],
    'properties': {
        'title': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['open', 'closed']},
    },
}

# A minimal legacy-YAML-dialect schema object (no `_isJsonSchema` stamp) —
# mirrors _LEGACY_BUG_SCHEMA in test_schema_validate.py.
_LEGACY_SCHEMA_OBJ = {
    'schema': 'fixture-legacy-schema',
    'required': {
        'title': 'string',
        'status': {'type': 'enum', 'values': ['open', 'closed']},
    },
    'optional': {
        'severity': {'type': 'enum', 'values': ['P0', 'P1', 'P2']},
    },
}


class TestValidateFrontmatterObjJsonSchemaBacked:
    def test_valid_fields_ok(self):
        result = validate_frontmatter_obj({'title': 'A thing', 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result == {'ok': True}

    def test_missing_required_field_rejected(self):
        result = validate_frontmatter_obj({'title': 'A thing'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False
        assert any(
            e['field'] == 'status' and e['error'] == 'required field missing'
            for e in result['errors']
        )

    def test_invalid_enum_value_rejected(self):
        result = validate_frontmatter_obj(
            {'title': 'A thing', 'status': 'not-a-status'}, _JSON_SCHEMA_OBJ
        )
        assert result['ok'] is False
        assert any(e['field'] == 'status' for e in result['errors'])
        assert set(result['errors'][0].keys()) == {'field', 'error', 'hint'}


class TestValidateFrontmatterObjTypeStringDateAcceptanceRegression:
    """Standing-ruling pin (DoE PM + claude-klabauter PM, cross-repo memo 2026-08-06):
    a `datetime.date`/`datetime.datetime` value against a declared
    `type: string` field must still validate — PyYAML's bare-date coercion
    feeds this validator ~700 artifacts' worth of date-typed values across
    both trees. Must NOT regress alongside the schema-valued
    additionalProperties fix.
    """

    def test_date_value_accepted_for_declared_type_string(self):
        result = validate_frontmatter_obj(
            {'title': datetime.date(2026, 8, 6), 'status': 'open'}, _JSON_SCHEMA_OBJ
        )
        assert result == {'ok': True}

    def test_datetime_value_accepted_for_declared_type_string(self):
        result = validate_frontmatter_obj(
            {'title': datetime.datetime(2026, 8, 6, 12, 0, 0), 'status': 'open'}, _JSON_SCHEMA_OBJ
        )
        assert result == {'ok': True}

    def test_int_value_rejected_for_declared_type_string(self):
        result = validate_frontmatter_obj({'title': 42, 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False

    def test_bool_value_rejected_for_declared_type_string(self):
        result = validate_frontmatter_obj({'title': True, 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False

    def test_list_value_rejected_for_declared_type_string(self):
        result = validate_frontmatter_obj({'title': ['a'], 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False

    def test_dict_value_rejected_for_declared_type_string(self):
        result = validate_frontmatter_obj({'title': {'a': 'b'}, 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False

    def test_float_value_rejected_for_declared_type_string(self):
        result = validate_frontmatter_obj({'title': 4.2, 'status': 'open'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False


class TestValidateFrontmatterObjLegacyYamlDialect:
    def test_valid_fields_ok(self):
        result = validate_frontmatter_obj({'title': 'A bug', 'status': 'open'}, _LEGACY_SCHEMA_OBJ)
        assert result == {'ok': True}

    def test_missing_required_field_rejected(self):
        result = validate_frontmatter_obj({'title': 'A bug'}, _LEGACY_SCHEMA_OBJ)
        assert result['ok'] is False
        assert any(
            e['field'] == 'status' and e['error'] == 'required field missing'
            for e in result['errors']
        )

    def test_optional_field_present_invalid_rejected(self):
        result = validate_frontmatter_obj(
            {'title': 'A bug', 'status': 'open', 'severity': 'P9'}, _LEGACY_SCHEMA_OBJ
        )
        assert result['ok'] is False
        assert any(e['field'] == 'severity' for e in result['errors'])


class TestValidateFrontmatterObjUnstampedSchemaDispatch:
    """An unstamped-but-genuine JSON Schema must reach the JSON-Schema
    validator, not the legacy YAML branch.

    `_isJsonSchema` is stamped only by load_schemas() on a .schema.json file.
    This seam's contract is that the CALLER owns schema provenance, so a
    derived, hand-built, or JSON round-tripped schema object arrives without
    the stamp while still being a JSON Schema. Routing those to the legacy
    branch was a silent fail-open: that branch treats "no `required` MAPPING"
    as unconditionally valid, and a JSON Schema's `required` is an ARRAY.
    """

    # _JSON_SCHEMA_OBJ minus the load-time stamp — e.g. what
    # json.loads(json.dumps(schema)) yields from a corpus that never went
    # through load_schemas, or what a caller filtering allOf branches holds.
    _UNSTAMPED_JSON_SCHEMA_OBJ = {
        k: v for k, v in _JSON_SCHEMA_OBJ.items() if k != '_isJsonSchema'
    }

    def test_unstamped_json_schema_still_enforces_required(self):
        result = validate_frontmatter_obj({'title': 'A thing'}, self._UNSTAMPED_JSON_SCHEMA_OBJ)
        assert result['ok'] is False
        assert any(
            e['field'] == 'status' and e['error'] == 'required field missing'
            for e in result['errors']
        )

    def test_unstamped_json_schema_still_enforces_enums(self):
        result = validate_frontmatter_obj(
            {'title': 'A thing', 'status': 'not-a-status'}, self._UNSTAMPED_JSON_SCHEMA_OBJ
        )
        assert result['ok'] is False
        assert any(e['field'] == 'status' for e in result['errors'])

    def test_unstamped_json_schema_accepts_valid_fields(self):
        result = validate_frontmatter_obj(
            {'title': 'A thing', 'status': 'open'}, self._UNSTAMPED_JSON_SCHEMA_OBJ
        )
        assert result == {'ok': True}

    def test_json_schema_without_top_level_required_is_not_a_blanket_pass_by_misroute(self):
        # The sharpest fail-open shape: no top-level `required`, so the legacy
        # branch would have returned {'ok': True} for anything. Classified
        # 'json' via `properties`, the enum constraint still bites.
        schema = {'type': 'object', 'properties': _JSON_SCHEMA_OBJ['properties']}
        assert validate_frontmatter_obj({'status': 'nope'}, schema)['ok'] is False
        assert validate_frontmatter_obj({'status': 'open'}, schema) == {'ok': True}

    def test_legacy_yaml_dialect_still_routes_to_legacy_branch(self):
        # The inference must not steal genuine YAML-dialect schemas: a
        # `required` MAPPING (not array) keeps them on the legacy path.
        result = validate_frontmatter_obj({'title': 'A bug', 'severity': 'P9'}, _LEGACY_SCHEMA_OBJ)
        assert result['ok'] is False
        # Legacy-branch-specific error text; the JSON branch words it differently.
        assert any(e['hint'] == 'Add "status:" to frontmatter' for e in result['errors'])

    def test_stamp_wins_over_shape_when_present(self):
        # A stamped object dispatches JSON-side even if its shape is
        # otherwise ambiguous — registry-resolved provenance is authoritative,
        # which is what keeps validate()'s path bit-for-bit unchanged.
        result = validate_frontmatter_obj({'anything': 'goes'}, {'_isJsonSchema': True})
        assert result == {'ok': True}


class TestValidateFrontmatterObjNeverRaises:
    def test_none_schema_obj_returns_error_result(self):
        result = validate_frontmatter_obj({'title': 'x'}, None)
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_schema'

    def test_non_dict_schema_obj_returns_error_result(self):
        result = validate_frontmatter_obj({'title': 'x'}, ['not', 'a', 'schema'])
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_schema'

    def test_non_dict_fm_dict_returns_error_result(self):
        result = validate_frontmatter_obj('not-a-dict', _JSON_SCHEMA_OBJ)
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_schema'

    def test_none_fm_dict_treated_as_empty_not_a_raise(self):
        # Parity with validate()/_dispatch_validate: fields=None coerces to {}
        # rather than raising — a JSON-Schema-backed schema with required
        # fields then reports them missing, not a TypeError.
        result = validate_frontmatter_obj(None, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False
        assert any(e['error'] == 'required field missing' for e in result['errors'])

    def test_no_recognizable_shape_returns_error_result_not_unconditional_pass(self):
        # A dict matching neither dialect's tells is REJECTED, not dispatched
        # into the legacy branch (whose "no required block" == "everything
        # passes" negative-spec would return {'ok': True} for any document).
        # Fail-closed on an undecidable schema; see _classify_schema_dialect.
        result = validate_frontmatter_obj({'anything': 'goes'}, {'no_recognizable_shape': True})
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_schema'

    def test_empty_schema_obj_returns_error_result(self):
        result = validate_frontmatter_obj({'title': 'x'}, {})
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_schema'

    def test_internal_exception_from_dispatch_caught_as_error_result(self, monkeypatch):
        import coordinator_core.frontmatter.schema_validate as sv

        def _boom(schema, fields):
            raise SchemaVersionError('simulated schema_version mismatch')

        monkeypatch.setattr(sv, '_dispatch_validate', _boom)
        result = sv.validate_frontmatter_obj({'title': 'x'}, _JSON_SCHEMA_OBJ)
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '_internal'
        assert 'simulated schema_version mismatch' in result['errors'][0]['error']


class TestValidateFrontmatterObjNoDiskAccess:
    def test_never_touches_claude_klabauter_vendored_schemas_dir(self, monkeypatch):
        """schema_obj is caller-owned — validate_frontmatter_obj must never call
        load_schemas() or resolve _SCHEMAS_DIR itself. Patch load_schemas to
        raise if invoked; a passing call proves no schema-directory read
        occurred for either dispatch branch."""
        import coordinator_core.frontmatter.schema_validate as sv

        def _load_schemas_should_not_be_called(*_args, **_kwargs):
            raise AssertionError('validate_frontmatter_obj must not call load_schemas()')

        monkeypatch.setattr(sv, 'load_schemas', _load_schemas_should_not_be_called)

        assert validate_frontmatter_obj(
            {'title': 'A thing', 'status': 'open'}, _JSON_SCHEMA_OBJ
        ) == {'ok': True}
        assert validate_frontmatter_obj(
            {'title': 'A bug', 'status': 'open'}, _LEGACY_SCHEMA_OBJ
        ) == {'ok': True}
