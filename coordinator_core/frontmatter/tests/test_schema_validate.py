"""
Tests for coordinator_core.frontmatter.schema_validate.

Coverage targets (per task spec):
  - validate_frontmatter: happy path, required-field missing, enum violation
  - cross-field rules: summary >140 chars, deployment_state gates, spinoff-predecessor,
    category/summary required on post-cutoff handoffs, shipped_in, consumed_by,
    cost enum, roadmap_id kind-gate (blocks/blocked_by permitted on any kind),
    roadmap required fields, supersedes/forked_from kind-gate,
    additional_predecessors integrity, deliverable_id prefix, initiative non-empty
  - handoff-archived schema: no cross-field rules applied
  - schema_version fail-loud: raises SchemaVersionError on incompatible version
  - drift check: passes when matching coordinator-claude HEAD; raises SchemaDriftError on divergence
  - round-trip: mutation via primitives → yaml parse → validate_frontmatter
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

# Real-git spawn is load-bearing: the drift-check coverage (module docstring
# above) validates against a real coordinator-claude HEAD comparison -- a mocked git cannot
# exhibit true divergence/match against actual repo state. Per-test
# isolation for the fixtures that build throwaway repos.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
)
# Review: code-reviewer — F4: moved `import datetime` to stdlib group (was after local imports).

from coordinator_core.frontmatter.schema_validate import (
    _cf_carried_items_shape,
    _cf_plan_tasks_disposition_shape,
    _GLOB_OVERRIDES,
    _lint_collect_files_for_glob,
    _lint_is_sidecar_file,
    _plan_tasks_schema_without_pm_approved_required,
    _PLAN_TASKS_SCHEMA,
    _PLAN_TASKS_SCHEMA_DICT,
    _SCHEMAS_DIR as _CLAUDE_KLABAUTER_SCHEMAS_DIR,
    _validate_json_schema_node,
    _validate_legacy_field,
    _validate_legacy_yaml_frontmatter,
    load_schemas,
    match_schema,
    SchemaDriftError,
    SchemaVersionError,
    check_plan_tasks_ordering,
    check_schema_ahead_of_doe,
    check_schema_drift,
    describe,
    is_unowned,
    parse_frontmatter,
    parse_yaml,
    validate,
    validate_frontmatter,
    validate_frontmatter_obj,
    validate_memo_cross_fields,
    DIRECTION_BOTH,
    DIRECTION_WE_AHEAD,
    DIRECTION_WE_BEHIND,
    _infer_drift_direction,
    check_schema_drift_advisory,
    _parse_semver_tuple,
    _read_bump_class,
    _read_bump_note,
)
from coordinator_core.testing.doe_root import resolve_doe_root

# The former node-oracle differential/parity suites (schema.js / schema-cli.js
# byte-parity, de-node Gate A straggler conversion) were retired 2026-07-24 (D1 of
# docs/plans/2026-07-24-python-ize-claude-klabauter-bin-oracles-doe-forwards-to.md) alongside
# the oracles themselves — coordinator/bin/lib/schema.js and coordinator/bin/schema-cli.js
# no longer exist anywhere in this repo or coordinator-claude, so there is nothing left to diff
# against, frozen golden or otherwise. schema_validate.py's/schema_cli.py's remaining
# standalone coverage (describe/validate behavioral cases, legacy-YAML-dialect field
# validator, cross-field rules, drift checks below) is unaffected — none of it depended
# on the node comparison.

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_SCHEMAS_DIR = Path(__file__).parent.parent / 'schemas'
_HANDOFF_SCHEMA = _SCHEMAS_DIR / 'handoff.schema.json'
_HANDOFF_ARCHIVED_SCHEMA = _SCHEMAS_DIR / 'handoff-archived.schema.json'
_PLAN_SCHEMA = _SCHEMAS_DIR / 'plan.schema.json'
_SIZING_OBJECT_SCHEMA = _SCHEMAS_DIR / 'sizing-object.schema.json'
_RESEARCH_SYNTHESIS_SCHEMA = _SCHEMAS_DIR / 'research-synthesis.schema.json'
# Resolved via the canonical coordinator_core.testing.doe_root pointer-file
# resolver — NOT a relative-sibling-checkout guess. A hardcoded
# parents[N]/'coordinator-claude' walk hardcodes both a checkout depth and a literal
# directory name; it resolves on exactly one machine/clone-layout and
# silently fails (or resolves the WRONG tree) on any other. May be None when
# the machine is unregistered — callers guard with
# `_DOE_REPO is None or not _DOE_REPO.exists()`.
_doe_root_str = resolve_doe_root()
_DOE_REPO = Path(_doe_root_str) if _doe_root_str else None


# ---------------------------------------------------------------------------
# Helper: minimal valid handoff dict (post-cutoff so cross-field rules fire)
# ---------------------------------------------------------------------------

def _valid_handoff(**overrides) -> dict:
    """Minimal valid handoff dict that passes all validations."""
    base = {
        'title': 'Test handoff',
        'created': '2026-07-05',
        'branch': 'work/test/2026-07-05',
        'status': 'open',
        'predecessor': 'state/handoffs/2026-07-04-prior.md',
        'kind': 'session-handoff',
        'category': 'infra',
        'summary': 'Short valid summary for test handoff',
    }
    base.update(overrides)
    return base


def _valid_archived_handoff(**overrides) -> dict:
    """Minimal valid archived handoff dict."""
    base = {
        'title': 'Archived handoff',
        'created': '2026-07-05',
        'branch': 'work/test/2026-07-05',
        'status': 'claimed',
        'predecessor': 'state/handoffs/2026-07-04-prior.md',
    }
    base.update(overrides)
    return base


def _valid_plan(**overrides) -> dict:
    """Minimal valid plan dict (required: title, created, author, status)."""
    base = {
        'title': 'Test plan',
        'created': '2026-08-06',
        'author': 'test-em',
        'status': 'draft',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema file existence sanity
# ---------------------------------------------------------------------------

class TestSchemaFilesExist:
    def test_handoff_schema_present(self):
        assert _HANDOFF_SCHEMA.exists(), f'Schema not found: {_HANDOFF_SCHEMA}'

    def test_handoff_archived_schema_present(self):
        assert _HANDOFF_ARCHIVED_SCHEMA.exists(), f'Schema not found: {_HANDOFF_ARCHIVED_SCHEMA}'

    def test_handoff_schema_is_valid_json(self):
        data = json.loads(_HANDOFF_SCHEMA.read_text())
        assert data.get('x-schema-name') == 'handoff'

    def test_handoff_archived_schema_is_valid_json(self):
        data = json.loads(_HANDOFF_ARCHIVED_SCHEMA.read_text())
        assert data.get('x-schema-name') == 'handoff-archived'


# ---------------------------------------------------------------------------
# validate_frontmatter — happy path
# ---------------------------------------------------------------------------

class TestValidateFrontmatterHappyPath:
    def test_minimal_valid_handoff(self):
        errors = validate_frontmatter(_valid_handoff(), _HANDOFF_SCHEMA)
        assert errors == []

    def test_spinoff_valid(self):
        """A valid spinoff: predecessor=none, kind=spinoff."""
        fm = _valid_handoff(kind='spinoff', predecessor='none')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_minimal_valid_archived(self):
        errors = validate_frontmatter(_valid_archived_handoff(), _HANDOFF_ARCHIVED_SCHEMA)
        assert errors == []

    def test_extra_optional_fields_ok(self):
        fm = _valid_handoff(
            deployment_state='ready_to_fire',
            pickup_ready=True,
            session_goal='Ship the feature',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_pre_cutoff_handoff_no_category_required(self):
        """Handoffs before 2026-05-29 do not require category/summary."""
        fm = {
            'title': 'Old handoff',
            'created': '2026-05-01',
            'branch': 'main',
            'status': 'open',
            'predecessor': 'none',
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []


class TestHandoffIdPlaceholderPatternNarrow:
    """Review: code-reviewer (Finding 3, P2) — the placeholder-id-minting-guard
    narrow (`handoff.schema.json` MAJOR 4.0.0->5.0.0) had no negative-case
    tripwire: nothing asserted a placeholder-shaped id actually fails, or that
    a normal id still passes, so a future accidental widen of the pattern (or
    a refactor dropping the lookahead) would have no automated signal."""

    def test_placeholder_shaped_handoff_id_rejected(self):
        fm = _valid_handoff(handoff_id='hnd-placeholder-replace-with-x-abc123')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors != []

    def test_normal_handoff_id_accepted(self):
        fm = _valid_handoff(handoff_id='hnd-foo-bar-abc123')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []


# ---------------------------------------------------------------------------
# _validate_json_schema_node — `pattern` keyword
#
# Spec backlink: the `pattern` gap this class closes was found and escalated
# by TestHandoffIdPlaceholderPatternNarrow's xfail(strict=True) above (now a
# passing assertion) — every `pattern` in every schema this validator reads
# was previously decorative. Covers each pattern-bearing field CLASS named
# in the census (top-level scalar, array-item, nested gate_evidence[].ref),
# plus the negative-lookahead case named explicitly in the fix's spec.
# ---------------------------------------------------------------------------

class TestPatternKeyword:
    def test_negative_lookahead_placeholder_rejected(self):
        """hnd-placeholder-replace-with-x-abc123 must be REJECTED (lookahead)."""
        fm = _valid_handoff(handoff_id='hnd-placeholder-replace-with-x-abc123')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors != []

    def test_negative_lookahead_real_title_accepted(self):
        """hnd-real-title-abc123 must be ACCEPTED (does not trip the lookahead)."""
        fm = _valid_handoff(handoff_id='hnd-real-title-abc123')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_top_level_scalar_execution_authorized_sha_accept(self):
        fm = _valid_handoff(execution_authorized_sha='a' * 40)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_top_level_scalar_execution_authorized_sha_reject(self):
        fm = _valid_handoff(execution_authorized_sha='not-a-sha!')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'execution_authorized_sha' in fields

    def test_array_item_blocked_by_accept(self):
        fm = _valid_handoff(blocked_by=['some-token'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_array_item_blocked_by_reject_path_shaped(self):
        """blocked_by items must not look like a repo-relative .md path."""
        fm = _valid_handoff(blocked_by=['state/handoffs/2026-07-04-prior.md'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert any(f.startswith('blocked_by') for f in fields)

    def test_nested_gate_evidence_ref_accept(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'leg-1',
                'kind': 'commit-sha',
                'repo': 'claude-klabauter',
                'ref': 'a' * 40,
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_nested_gate_evidence_ref_reject(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'leg-1',
                'kind': 'commit-sha',
                'repo': 'claude-klabauter',
                'ref': 'not-hex!',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert any('gate_evidence' in f for f in fields)

    def test_non_string_value_ignored(self):
        """A non-string value is not a pattern violation — it is ignored."""
        schema = {'type': ['string', 'null'], 'pattern': r'^[0-9]+$'}
        errors = _validate_json_schema_node(None, schema, schema, 'field')
        assert errors == []

    def test_uncompilable_pattern_fails_loud(self):
        schema = {'type': 'string', 'pattern': '[unterminated'}
        with pytest.raises(ValueError):
            _validate_json_schema_node('anything', schema, schema, 'field')

    def test_unanchored_partial_match_semantics(self):
        """pattern is unanchored per spec — a bare substring pattern with no
        ^/$ matches anywhere in the string (mirrors gate_evidence's
        test-node-id `::` pattern, which asserts containment, not shape)."""
        schema = {'type': 'string', 'pattern': '::'}
        assert _validate_json_schema_node('tests/foo.py::TestX::test_y', schema, schema, 'field') == []
        assert _validate_json_schema_node('no-separator-here', schema, schema, 'field') != []


# ---------------------------------------------------------------------------
# _validate_json_schema_node — `minLength`, `minItems`, `minimum`,
# `propertyNames` keywords
#
# Spec backlink: these four were found present-in-schema-but-unimplemented in
# the same code-review pass that surfaced TestPatternKeyword's gap above —
# same silently-decorative failure mode (an empty string / empty array /
# sub-minimum number / forbidden object key all passed validation exactly
# as a garbage `pattern` value used to).
# ---------------------------------------------------------------------------

class TestMinLengthKeyword:
    def test_top_level_scalar_accept(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'leg-1', 'kind': 'human', 'reason': 'not yet resolvable'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any('reason' in e['field'] and 'minLength' in e['error'] for e in errors)

    def test_nested_array_item_reject_empty_string(self):
        """gate_evidence.legs[].leg_id carries minLength: 1 — an empty string
        must be rejected at the nested array-item path."""
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': '', 'kind': 'human', 'reason': 'not yet resolvable'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert any('leg_id' in f for f in fields)

    def test_non_string_value_ignored(self):
        """A non-string value is not a minLength violation — it is ignored."""
        schema = {'type': ['string', 'null'], 'minLength': 1}
        errors = _validate_json_schema_node(None, schema, schema, 'field')
        assert errors == []

    def test_direct_accept_and_reject(self):
        schema = {'type': 'string', 'minLength': 3}
        assert _validate_json_schema_node('abc', schema, schema, 'field') == []
        assert _validate_json_schema_node('ab', schema, schema, 'field') != []


class TestMinItemsKeyword:
    def test_top_level_array_accept(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'leg-1', 'kind': 'human', 'reason': 'not yet resolvable'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any('legs' in e['field'] and 'minItems' in e['error'] for e in errors)

    def test_top_level_array_reject_empty(self):
        """gate_evidence.legs carries minItems: 1 — an empty array must reject."""
        fm = _valid_handoff(gate_evidence={'covers_prose': True, 'legs': []})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert any('legs' in f for f in fields)

    def test_non_array_value_ignored(self):
        """A non-array value is not a minItems violation — it is ignored."""
        schema = {'type': ['array', 'null'], 'minItems': 1}
        errors = _validate_json_schema_node(None, schema, schema, 'field')
        assert errors == []

    def test_direct_accept_and_reject(self):
        schema = {'type': 'array', 'minItems': 2}
        assert _validate_json_schema_node([1, 2], schema, schema, 'field') == []
        assert _validate_json_schema_node([1], schema, schema, 'field') != []


class TestMinimumKeyword:
    # Exercised against a synthetic schema rather than a live artifact schema:
    # handoff.schema.json's last `minimum` was carried_items[].carry_count,
    # removed with the carry counter (DR-268). The keyword still needs nested
    # array-item coverage regardless of which schema happens to use it.
    _NESTED = {
        'type': 'object',
        'properties': {
            'rows': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {'n': {'type': 'integer', 'minimum': 1}},
                },
            },
        },
    }

    def test_nested_array_item_accept(self):
        errors = _validate_json_schema_node({'rows': [{'n': 1}]}, self._NESTED, self._NESTED, 'fm')
        assert errors == []

    def test_nested_array_item_reject(self):
        errors = _validate_json_schema_node({'rows': [{'n': 0}]}, self._NESTED, self._NESTED, 'fm')
        fields = [e['field'] for e in errors]
        assert any('n' in f for f in fields)
        assert any('minimum' in e['error'] for e in errors)

    def test_bool_value_ignored(self):
        """A bool is not a minimum violation — JSON Schema numbers exclude
        booleans even though Python's bool subclasses int."""
        schema = {'type': ['boolean', 'integer'], 'minimum': 1}
        errors = _validate_json_schema_node(False, schema, schema, 'field')
        assert errors == []

    def test_direct_accept_and_reject(self):
        schema = {'type': 'integer', 'minimum': 1}
        assert _validate_json_schema_node(1, schema, schema, 'field') == []
        assert _validate_json_schema_node(0, schema, schema, 'field') != []

    # Review: code-reviewer (P2, accepted) — restores the integration-level leg
    # (validate_frontmatter + a real vendored schema) that the rewrite above
    # lost. research-synthesis.schema.json's `coverage_score` (minimum: 1,
    # maximum: 5) is the vendored `minimum` usage exercised here.
    def test_via_validate_frontmatter_real_schema_reject(self):
        errors = validate_frontmatter(
            {'coverage_score': 0}, _RESEARCH_SYNTHESIS_SCHEMA,
        )
        fields = [e['field'] for e in errors]
        assert any('coverage_score' in f for f in fields)

    def test_via_validate_frontmatter_real_schema_accept(self):
        errors = validate_frontmatter(
            {'coverage_score': 3}, _RESEARCH_SYNTHESIS_SCHEMA,
        )
        assert not any('coverage_score' in e['field'] for e in errors)


class TestPropertyNamesKeyword:
    def test_nested_array_item_accept(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'leg-1', 'kind': 'human', 'reason': 'not yet resolvable'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any('propertyNames' in e.get('error', '') for e in errors)

    def test_nested_array_item_reject_forbidden_key(self):
        """gate_evidence.legs[] is propertyNames-blocked from carrying the
        four resolver-only keys (read_ok/observed/error/elapsed) — an author
        writing one directly must be rejected."""
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'leg-1', 'kind': 'human', 'reason': 'not yet resolvable',
                'observed': True,
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert any('observed' in f for f in fields)

    def test_direct_accept_and_reject(self):
        schema = {'type': 'object', 'propertyNames': {'not': {'enum': ['forbidden']}}}
        assert _validate_json_schema_node({'ok': 1}, schema, schema, 'field') == []
        assert _validate_json_schema_node({'forbidden': 1}, schema, schema, 'field') != []


# ---------------------------------------------------------------------------
# validate_frontmatter — required fields
# ---------------------------------------------------------------------------

class TestRequiredFields:
    def test_missing_title(self):
        fm = _valid_handoff()
        del fm['title']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'title' in fields

    def test_missing_created(self):
        fm = _valid_handoff()
        del fm['created']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'created' in fields

    def test_missing_branch(self):
        fm = _valid_handoff()
        del fm['branch']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'branch' in fields

    def test_missing_status(self):
        fm = _valid_handoff()
        del fm['status']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'status' in fields

    def test_missing_predecessor(self):
        fm = _valid_handoff()
        del fm['predecessor']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        fields = [e['field'] for e in errors]
        assert 'predecessor' in fields

    def test_predecessor_null_is_valid(self):
        """predecessor: null is allowed (nullable per anyOf)."""
        fm = _valid_handoff(kind='spinoff', predecessor=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        # predecessor=null with kind=spinoff is valid
        assert not any(e['field'] == 'predecessor' for e in errors)


# ---------------------------------------------------------------------------
# validate_frontmatter — enum violations
# ---------------------------------------------------------------------------

class TestEnumValidation:
    def test_invalid_status(self):
        fm = _valid_handoff(status='invalid_status')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'status' for e in errors)

    def test_invalid_kind(self):
        fm = _valid_handoff(kind='unknown-kind')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'kind' for e in errors)

    def test_invalid_deployment_state(self):
        fm = _valid_handoff(deployment_state='invalid_state')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'deployment_state' for e in errors)

    def test_invalid_category(self):
        """Out-of-enum category fails exactly once (the JSON-schema enum node, not a
        duplicate cross-field rule) and its hint names the legal values, so a
        present-but-wrong value (e.g. "feature") is diagnosable from the message alone.

        Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
        """
        fm = _valid_handoff(category='totally-wrong')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        category_errors = [e for e in errors if e['field'] == 'category']
        assert len(category_errors) == 1, f'Expected exactly one category error, got: {errors}'
        hint = category_errors[0]['hint']
        for legal in ['roadmap', 'infra', 'bug', 'docs', 'research', 'refactor', 'uncategorized', 'queue-derived-baton']:
            assert legal in hint, f'Expected hint to name "{legal}", got: {hint}'

    def test_valid_kind_values(self):
        """Covers all six D1 canonical kinds (baton-kind-vocabulary-one-axis-per-field C10) —
        session-handoff/spinoff/recovery were already covered pre-migration; roadmap-baton,
        goal-seed, and roadmap-seed are the D1 target names that replaced the retired
        spinoff-roadmap/spinoff-goal/spinoff-roadmap-creator spellings and previously had no
        positive coverage here.

        Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § D1
        """
        for kind in ['session-handoff', 'spinoff', 'roadmap-baton', 'goal-seed', 'roadmap-seed', 'recovery']:
            if kind in ('spinoff', 'goal-seed', 'roadmap-seed'):
                fm = _valid_handoff(kind=kind, predecessor='none')
            elif kind == 'roadmap-baton':
                fm = _valid_handoff(
                    kind=kind,
                    roadmap_id='r-001',
                    stub_id='foo-1',
                    wave=1,
                    blocks=[],
                    blocked_by=[],
                )
            else:
                fm = _valid_handoff(kind=kind)
            errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
            kind_errors = [e for e in errors if e['field'] == 'kind']
            assert kind_errors == [], f'kind={kind} got unexpected errors: {kind_errors}'


# ---------------------------------------------------------------------------
# validate_frontmatter — type violations
# ---------------------------------------------------------------------------

class TestTypeValidation:
    def test_pickup_ready_must_be_boolean(self):
        fm = _valid_handoff(pickup_ready='yes')  # string, not bool
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'pickup_ready' for e in errors)

    def test_scope_must_be_array(self):
        fm = _valid_handoff(scope='not-a-list')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any('scope' in e['field'] for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — summary length cap (the key post-mutation gate)
# ---------------------------------------------------------------------------

class TestSummaryLengthCap:
    def test_summary_within_140_chars(self):
        fm = _valid_handoff(summary='x' * 140)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'summary' for e in errors)

    def test_summary_exactly_141_chars_fails(self):
        """Over-140-char summary is the canonical use case for post-mutation validation."""
        fm = _valid_handoff(summary='x' * 141)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'summary' and '140' in e['error'] for e in errors)

    def test_summary_200_chars_fails_with_count(self):
        fm = _valid_handoff(summary='a' * 200)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        summary_errors = [e for e in errors if e['field'] == 'summary' and 'exceeds' in e['error']]
        assert summary_errors
        assert '200' in summary_errors[0]['error']

    def test_summary_cap_skipped_pre_cutoff(self):
        """Pre-cutoff handoffs exempt from summary length rule."""
        fm = {
            'title': 'Old',
            'created': '2026-01-01',
            'branch': 'main',
            'status': 'open',
            'predecessor': 'none',
            'summary': 'x' * 200,  # would fail for post-cutoff
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'summary' and 'exceeds' in e['error'] for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — category/summary required post-cutoff
# ---------------------------------------------------------------------------

class TestPostCutoffRequired:
    def test_category_required_post_cutoff(self):
        fm = _valid_handoff()
        del fm['category']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'category' for e in errors)

    def test_summary_required_post_cutoff(self):
        fm = _valid_handoff()
        del fm['summary']
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'summary' for e in errors)

    def test_category_empty_string_fails(self):
        fm = _valid_handoff(category='')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'category' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — deployment_state gates
# ---------------------------------------------------------------------------

class TestDeploymentStateGates:
    def test_awaiting_gate_requires_dependency(self):
        fm = _valid_handoff(deployment_state='awaiting_gate')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_dependency' and 'awaiting_gate' in e['error'] for e in errors)

    def test_awaiting_gate_with_dependency_ok(self):
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='pcore-01 landing')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_awaiting_gate_with_blocked_by_ok(self):
        """C3: an awaiting_gate baton with an empty gate_dependency but a non-empty
        blocked_by list validates -- blocked_by is one of the three gate-naming fields."""
        fm = _valid_handoff(deployment_state='awaiting_gate', blocked_by=['stub-1'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_awaiting_gate_with_blocking_notes_ok(self):
        """C3: an awaiting_gate baton with an empty gate_dependency but a non-empty
        blocking_notes string validates -- the migration target field."""
        fm = _valid_handoff(deployment_state='awaiting_gate', blocking_notes='waiting on pcore-01')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_awaiting_gate_with_none_of_the_three_fails(self):
        """C3: an awaiting_gate baton with none of gate_dependency, blocked_by, or
        blocking_notes still fails -- the whole point of the rule surviving relaxation."""
        fm = _valid_handoff(deployment_state='awaiting_gate', blocked_by=[], blocking_notes='')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_dependency' and 'awaiting_gate' in e['error'] for e in errors)

    def test_ready_to_fire_with_dependency_fails(self):
        fm = _valid_handoff(deployment_state='ready_to_fire', gate_dependency='some-condition')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_dependency' and 'ready_to_fire' in e['error'] for e in errors)

    def test_ready_to_fire_no_dependency_ok(self):
        fm = _valid_handoff(deployment_state='ready_to_fire')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_ready_to_fire_with_gate_evidence_fails(self):
        """C7 (AC10): parallels test_ready_to_fire_with_dependency_fails --
        gate_evidence is a gate-naming field exactly like gate_dependency, and
        must not survive onto a ready_to_fire record either."""
        fm = _valid_handoff(
            deployment_state='ready_to_fire',
            gate_evidence={'legs': [{'kind': 'human', 'reason': 'manual check'}]},
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence' and 'ready_to_fire' in e['error'] for e in errors)

    def test_ready_to_fire_no_gate_evidence_ok(self):
        fm = _valid_handoff(deployment_state='ready_to_fire')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_evidence' for e in errors)

    def test_shipped_without_shipped_in_fails(self):
        fm = _valid_handoff(deployment_state='shipped')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'shipped_in' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — gate_dependency must not be path-shaped (C2b, dialect D3)
# ---------------------------------------------------------------------------

class TestGateDependencyNotPathShaped:
    def test_gate_dependency_containing_tasks_slash_fails(self):
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='tasks/foo/todo.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'gate_dependency' and 'file path' in e['error']
            for e in errors
        )

    def test_gate_dependency_containing_archive_slash_fails(self):
        fm = _valid_handoff(
            deployment_state='awaiting_gate',
            gate_dependency='archive/completed/2026-07/some-handoff.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'gate_dependency' and 'file path' in e['error']
            for e in errors
        )

    def test_gate_dependency_ending_in_md_fails(self):
        fm = _valid_handoff(
            deployment_state='awaiting_gate',
            gate_dependency='state/handoffs/2026-07-27-some-handoff.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'gate_dependency' and 'file path' in e['error']
            for e in errors
        )

    def test_gate_dependency_ordinary_prose_ok(self):
        """Ordinary advisory prose (no path markers) still validates."""
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='pcore-01 landing')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_gate_dependency_slug_shaped_ok(self):
        """A slug-shaped gate_dependency (the blocked_by-style form) still validates."""
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='pcore-01-landing')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_shipped_with_shipped_in_ok(self):
        fm = _valid_handoff(deployment_state='shipped', shipped_in='abc1234')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'shipped_in' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — spinoff predecessor rule (A3a-3)
# ---------------------------------------------------------------------------

class TestReadyToFireIfThenSchemaLevel:
    """JSON-Schema-level (if/then) enforcement of ready_to_fire -> gate_dependency-forbidden
    is now LIVE again — this class documents the allOf-nested conditional firing and its
    (now redundant, not sole) relationship to the Python cross-field rule.

    Isolates the schema's own if/then construct from the Python cross-field rule
    (_cf_ready_to_fire_no_dependency in schema_validate.py) by calling
    _validate_json_schema_node directly — the Python cross-field layer is never
    invoked in this test class.

    History: C1's schema-hardening sub-step
    (docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C1) originally added
    this if/then construct as a claude-klabauter-local addition, later dropped and re-added
    upstream by coordinator-claude at bfbaac70 as a top-level `if`/`then` pair. DR-084's P4 narrow
    (d652253c) restructured it into one of four conditionals nested under a new
    top-level `allOf` array (alongside closed=>closed_reason, continued=>continued_into,
    claimed+claimed_at=>claimed_by). `_validate_json_schema_node` — a dependency-free
    subset port of coordinator-claude's schema.js — did not recurse into `allOf` until the
    schema-validator-keyword-gap fix
    (cross-repo/inbox/2026-07-25-coordinator-claude-em-schema-validator-keyword-gap.md), so none
    of the four allOf-nested conditionals fired at the schema-shape layer for a window.
    That gap is now closed: `allOf` is a dispatched keyword, so this construct fires
    again at the shape layer, same as it did before the DR-084 P4 restructuring — this
    is now belt-and-suspenders alongside `_cf_ready_to_fire_no_dependency` (Python
    cross-field rule, exercised via `validate_frontmatter` by TestDeploymentStateGates
    above), not the sole enforcement, but no longer inert either.
    """

    def _schema_shape_errors(self, fm: dict) -> list:
        schema = json.loads(_HANDOFF_SCHEMA.read_text(encoding='utf-8'))
        return _validate_json_schema_node(fm, schema, schema, '')

    def test_if_then_construct_present_and_allof_nested(self):
        """The schema declares its conditionals nested under `allOf`, not top-level
        `if`/`then` (DR-084 P4 narrow, d652253c) — `_validate_json_schema_node` now
        recurses into `allOf`, so these ARE visible to the shape-only validator."""
        schema = json.loads(_HANDOFF_SCHEMA.read_text(encoding='utf-8'))
        assert 'if' not in schema and 'then' not in schema, (
            'handoff.schema.json declares a top-level if/then again — the P4-narrow '
            'allOf restructuring (d652253c) may have been reverted upstream, revisit'
        )
        assert 'allOf' in schema and isinstance(schema['allOf'], list) and len(schema['allOf']) >= 1, (
            'handoff.schema.json no longer nests its conditionals under allOf — '
            'this test\'s premise (coordinator-claude\'s P4 narrow, d652253c) may be stale, revisit'
        )
        assert any('if' in clause and 'then' in clause for clause in schema['allOf']), (
            'no allOf clause carries an if/then pair — the ready_to_fire conditional '
            'construct may have been dropped upstream, revisit'
        )

    def test_gate_dependency_at_ready_to_fire_rejected_at_schema_level(self):
        """The allOf-nested if/then now fires at the shape layer (see class docstring) —
        redundant with, not a replacement for, `_cf_ready_to_fire_no_dependency` (Python
        cross-field layer), which remains the primary enforcement covered separately."""
        fm = _valid_handoff(deployment_state='ready_to_fire', gate_dependency='some-condition')
        errors = self._schema_shape_errors(fm)
        assert any('must NOT match the negated schema' in e['error'] for e in errors), (
            f'expected the allOf-nested if/then to fire at the shape layer now that allOf '
            f'is dispatched; got {errors!r}'
        )

    def test_if_then_allows_no_gate_dependency_at_ready_to_fire(self):
        fm = _valid_handoff(deployment_state='ready_to_fire')
        errors = self._schema_shape_errors(fm)
        assert not any('must NOT match the negated schema' in e['error'] for e in errors)

    def test_if_then_does_not_fire_at_other_deployment_states(self):
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='some-condition')
        errors = self._schema_shape_errors(fm)
        assert not any('must NOT match the negated schema' in e['error'] for e in errors)

    def test_if_then_does_not_fire_when_deployment_state_absent(self):
        fm = _valid_handoff(gate_dependency='some-condition')
        fm.pop('deployment_state', None)
        errors = self._schema_shape_errors(fm)
        assert not any('must NOT match the negated schema' in e['error'] for e in errors)


class TestLastGateRecheckSchemaDeclaration:
    """last_gate_recheck IS declared as a schema property again, upstream-owned by
    coordinator-claude rather than claude-klabauter-local.

    History: C1's schema-hardening sub-step
    (docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C1) originally added
    a JSON-Schema-level (type: string, format: date) declaration as a claude-klabauter-local
    addition. The DR-084 P0 dual-vocabulary re-vendor (03b8a127, from coordinator-claude HEAD
    6082a287) replaced the vendored file wholesale and dropped it — a straight
    re-vendor, not a merge. Claude-klabauter memo'd coordinator-claude asking for the property to be re-added
    upstream; coordinator-claude landed it at bfbaac70 ("schema: re-add last_gate_recheck property +
    ready_to_fire if/then dropped by DR-084 P0 widen (claude-klabauter memo)"). This re-vendor
    (pulling in bfbaac70) restores the declaration, now as an upstream-owned coordinator-claude
    property rather than claude-klabauter-local drift. last_gate_recheck remains a real,
    actively-written field (see coordinator_core/ops/handoff_transition.py,
    coordinator_core/ops/handoff_gate_aging.py) and is once again schema-validated
    for date-format shape. This test guards against a future re-vendor silently
    losing the declaration again.
    """

    def test_last_gate_recheck_valid_date_ok(self):
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='x',
                             last_gate_recheck='2026-07-13')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'last_gate_recheck' for e in errors)

    def test_last_gate_recheck_invalid_date_rejected_at_schema_level(self):
        fm = _valid_handoff(deployment_state='awaiting_gate', gate_dependency='x',
                             last_gate_recheck='not-a-date')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'last_gate_recheck' for e in errors), (
            'expected a schema-level format rejection post-coordinator-claude-readd (property '
            'declared with format: date); got no last_gate_recheck error'
        )

    def test_last_gate_recheck_declared_as_schema_property(self):
        schema = json.loads(_HANDOFF_SCHEMA.read_text(encoding='utf-8'))
        assert 'last_gate_recheck' in schema['properties'], (
            'handoff.schema.json no longer declares last_gate_recheck — this test\'s '
            'premise (coordinator-claude re-added the property at bfbaac70) may be stale, revisit'
        )


class TestSpinoffPredecessor:
    def test_spinoff_with_non_none_predecessor_fails(self):
        fm = _valid_handoff(kind='spinoff', predecessor='state/handoffs/2026-07-04-prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'predecessor' and 'none' in e['error'] for e in errors)

    def test_spinoff_with_none_predecessor_ok(self):
        fm = _valid_handoff(kind='spinoff', predecessor='none')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'predecessor' for e in errors)

    def test_spinoff_with_null_predecessor_ok(self):
        fm = _valid_handoff(kind='spinoff', predecessor=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'predecessor' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — claimed_by required (DR-084 A3a-1, new vocabulary)
# ---------------------------------------------------------------------------

class TestClaimedByRequired:
    def test_claimed_with_claimed_at_needs_claimed_by(self):
        fm = _valid_handoff(
            status='claimed',
            claimed_at='2026-07-05T10:00:00Z',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'claimed_by' for e in errors)

    def test_claimed_with_claimed_at_and_claimed_by_ok(self):
        fm = _valid_handoff(
            status='claimed',
            claimed_at='2026-07-05T10:00:00Z',
            claimed_by='sess-abc123',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'claimed_by' for e in errors)

    def test_claimed_without_claimed_at_no_claimed_by_needed(self):
        """status=claimed without claimed_at: pre-tool claim, no claimed_by required."""
        fm = _valid_handoff(status='claimed')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'claimed_by' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — closed_reason required (DR-084, bidirectional)
# ---------------------------------------------------------------------------

class TestClosedReasonRequired:
    def test_closed_without_closed_reason_errors(self):
        fm = _valid_handoff(deployment_state='closed')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'closed_reason' for e in errors)

    def test_closed_with_closed_reason_ok(self):
        fm = _valid_handoff(deployment_state='closed', closed_reason='cancelled')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'closed_reason' for e in errors)

    def test_closed_reason_without_closed_state_errors(self):
        """closed_reason set but deployment_state != closed: forbidden (bidirectional)."""
        fm = _valid_handoff(deployment_state='in_flight', closed_reason='stale')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'closed_reason' for e in errors)

    def test_no_closed_reason_no_closed_state_ok(self):
        fm = _valid_handoff(deployment_state='in_flight')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'closed_reason' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — continued_into required (DR-084)
# ---------------------------------------------------------------------------

class TestContinuedIntoRequired:
    def test_continued_without_continued_into_errors(self):
        fm = _valid_handoff(deployment_state='continued')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'continued_into' for e in errors)

    def test_continued_with_continued_into_ok(self):
        fm = _valid_handoff(
            deployment_state='continued',
            continued_into='hnd-successor-abc123',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'continued_into' for e in errors)

    def test_other_deployment_state_no_continued_into_needed(self):
        fm = _valid_handoff(deployment_state='in_flight')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'continued_into' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — cost enum
# ---------------------------------------------------------------------------

class TestCostEnum:
    def test_valid_cost_values(self):
        for cost in ['T0', 'T1', 'T2', 'T3']:
            fm = _valid_handoff(cost=cost)
            errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
            assert not any(e['field'] == 'cost' for e in errors), f'cost={cost} should be valid'

    def test_invalid_cost_value(self):
        fm = _valid_handoff(cost='T4')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'cost' for e in errors)

    def test_absent_cost_ok(self):
        """cost is optional — absent is valid."""
        fm = _valid_handoff()
        assert 'cost' not in fm
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'cost' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — graph primitives kind-gate (roadmap-baton only)
# ---------------------------------------------------------------------------

class TestGraphFieldsKindGate:
    def test_blocks_on_non_roadmap_kind_ok(self):
        """AC1(a): blocks/blocked_by are permitted on any kind, not just roadmap-baton."""
        fm = _valid_handoff(blocks=['stub-1'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'blocks' for e in errors)

    def test_blocked_by_on_non_roadmap_kind_ok(self):
        """AC1(a): blocks/blocked_by are permitted on any kind, not just roadmap-baton."""
        fm = _valid_handoff(blocked_by=['stub-2'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'blocked_by' for e in errors)

    def test_roadmap_id_on_non_roadmap_kind_fails(self):
        fm = _valid_handoff(roadmap_id='r-001')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any('roadmap_id' in e['field'] for e in errors)

    def test_roadmap_baton_missing_required_fields_fails(self):
        fm = _valid_handoff(kind='roadmap-baton', predecessor=None, roadmap_id='r-001')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        # Missing stub_id, wave, blocks, blocked_by
        assert any('stub_id' in e['field'] or 'wave' in e['field'] for e in errors)

    def test_roadmap_baton_legacy_alias_missing_required_fields_fails(self):
        """Legacy-alias coverage (baton-kind-vocabulary-one-axis-per-field D1): the
        cross-field graph-primitives rule still fires identically for the retired
        `spinoff-roadmap` spelling — dual acceptance is enforced at the cross-field
        layer even though the JSON-schema enum itself now rejects the retired kind
        value outright (see the `kind`-field enum error co-occurring here, which this
        assertion deliberately does not check for)."""
        fm = _valid_handoff(kind='spinoff-roadmap', predecessor=None, roadmap_id='r-001')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        # Missing stub_id, wave, blocks, blocked_by
        assert any('stub_id' in e['field'] or 'wave' in e['field'] for e in errors)

    def test_roadmap_baton_complete_ok(self):
        fm = _valid_handoff(
            kind='roadmap-baton',
            predecessor=None,
            roadmap_id='r-001',
            stub_id='foo-1',
            wave=1,
            blocks=[],
            blocked_by=[],
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        # No graph-field or roadmap-specific errors expected
        assert not any(
            e['field'] in ('roadmap_id', 'stub_id', 'wave') for e in errors
        )

    def test_roadmap_baton_legacy_alias_graph_fields_ok(self):
        """Legacy-alias coverage: cross-field graph-primitives rule accepts the retired
        `spinoff-roadmap` spelling identically to `roadmap-baton`. Filters on the
        graph-field errors only (not `kind`) because the JSON-schema enum rejects the
        retired spelling — a separate FINDING, not something this test papers over."""
        fm = _valid_handoff(
            kind='spinoff-roadmap',
            predecessor=None,
            roadmap_id='r-001',
            stub_id='foo-1',
            wave=1,
            blocks=[],
            blocked_by=[],
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(
            e['field'] in ('roadmap_id', 'stub_id', 'wave') for e in errors
        )


# ---------------------------------------------------------------------------
# Cross-field rules — handoff_phase kind-gate (H-CROSS-EXEC-2)
# ---------------------------------------------------------------------------

class TestHandoffPhaseKindGate:
    """`handoff_phase` is admitted on kind: session-handoff AND canonical
    kind: roadmap-baton (coordinator-claude DR-126; schema description `feef6527f`).

    The retired-alias case is the load-bearing one: real roadmap batons on disk
    carry `kind: spinoff-roadmap`, so a gate written against the canonical name
    alone would never fire on one.
    """

    def test_session_handoff_ok(self):
        fm = _valid_handoff(handoff_phase='continuation')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'handoff_phase' for e in errors)

    def test_roadmap_baton_canonical_ok(self):
        fm = _valid_handoff(
            kind='roadmap-baton',
            predecessor=None,
            roadmap_id='r-001',
            stub_id='foo-1',
            wave=1,
            blocks=[],
            blocked_by=[],
            handoff_phase='continuation',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'handoff_phase' for e in errors)

    def test_roadmap_baton_retired_alias_ok(self):
        """Filters on `handoff_phase` only — the JSON-schema `kind` enum rejects the
        retired spelling outright, which this assertion deliberately does not check
        for (same posture as `TestGraphFieldsKindGate`'s alias cases)."""
        fm = _valid_handoff(
            kind='spinoff-roadmap',
            predecessor=None,
            roadmap_id='r-001',
            stub_id='foo-1',
            wave=1,
            blocks=[],
            blocked_by=[],
            handoff_phase='execution',
            execution_authorized_by='PM',
            execution_authorized_at='2026-08-03T00:00:00Z',
            execution_authorized_sha='0' * 40,
            execution_authorized_note='authorized',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'handoff_phase' for e in errors)

    def test_unrelated_kind_still_fails(self):
        fm = _valid_handoff(kind='spinoff', predecessor=None, handoff_phase='continuation')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'handoff_phase' for e in errors)

    def test_unset_kind_still_fails(self):
        fm = _valid_handoff(handoff_phase='continuation')
        fm.pop('kind')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'handoff_phase' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — supersedes / forked_from kind-gate
# ---------------------------------------------------------------------------

class TestKindGatedFields:
    def test_supersedes_on_non_spinoff_fails(self):
        fm = _valid_handoff(supersedes='some/older/baton.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'supersedes' and 'spinoff' in e['error'] for e in errors)

    def test_supersedes_on_spinoff_ok(self):
        fm = _valid_handoff(kind='spinoff', predecessor='none', supersedes='state/handoffs/old.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'supersedes' for e in errors)

    def test_forked_from_on_non_spinoff_fails(self):
        fm = _valid_handoff(forked_from='state/handoffs/fork-point.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'forked_from' for e in errors)

    def test_forked_from_on_spinoff_ok(self):
        fm = _valid_handoff(kind='spinoff', predecessor='none', forked_from='state/handoffs/fork.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'forked_from' for e in errors)

    def test_forked_from_on_goal_seed_fails(self):
        """Negative-spec: goal-seed is a fork kind (predecessor:none) but is excluded
        from the forked_from allowlist — PM-directive origin, no baton branch-point."""
        fm = _valid_handoff(kind='goal-seed', predecessor='none', forked_from='state/handoffs/fork.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'forked_from' for e in errors)

    def test_forked_from_on_goal_seed_legacy_alias_fails(self):
        """Legacy-alias coverage: the retired `spinoff-goal` spelling is excluded from
        forked_from identically to `goal-seed` — cross-field dual acceptance."""
        fm = _valid_handoff(kind='spinoff-goal', predecessor='none', forked_from='state/handoffs/fork.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'forked_from' for e in errors)

    def test_forked_from_on_roadmap_seed_fails(self):
        """Negative-spec: roadmap-seed likewise excluded from forked_from."""
        fm = _valid_handoff(
            kind='roadmap-seed', predecessor='none', forked_from='state/handoffs/fork.md'
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'forked_from' for e in errors)

    def test_forked_from_on_roadmap_seed_legacy_alias_fails(self):
        """Legacy-alias coverage: the retired `spinoff-roadmap-creator` spelling is
        excluded from forked_from identically to `roadmap-seed`."""
        fm = _valid_handoff(
            kind='spinoff-roadmap-creator', predecessor='none', forked_from='state/handoffs/fork.md'
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'forked_from' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — spinoffKinds extension (Rule A3a-3 sister kinds)
# ---------------------------------------------------------------------------

class TestForkKindsPredecessorNoneGate:
    """predecessor:none gate extends to goal-seed and roadmap-seed (D1 canonical
    names for the retired spinoff-goal/spinoff-roadmap-creator spellings, both of
    which remain permanently-accepted aliases at the cross-field layer)."""

    def test_goal_seed_with_non_none_predecessor_fails(self):
        fm = _valid_handoff(kind='goal-seed', predecessor='state/handoffs/prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'predecessor' and 'goal-seed' in e['error'] for e in errors)

    def test_goal_seed_legacy_alias_with_non_none_predecessor_fails(self):
        fm = _valid_handoff(kind='spinoff-goal', predecessor='state/handoffs/prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'predecessor' and 'spinoff-goal' in e['error'] for e in errors)

    def test_goal_seed_with_none_predecessor_ok(self):
        fm = _valid_handoff(kind='goal-seed', predecessor='none')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'predecessor' for e in errors)

    def test_roadmap_seed_with_non_none_predecessor_fails(self):
        fm = _valid_handoff(kind='roadmap-seed', predecessor='state/handoffs/prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'predecessor' and 'roadmap-seed' in e['error'] for e in errors
        )

    def test_roadmap_seed_legacy_alias_with_non_none_predecessor_fails(self):
        fm = _valid_handoff(kind='spinoff-roadmap-creator', predecessor='state/handoffs/prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'predecessor' and 'spinoff-roadmap-creator' in e['error'] for e in errors
        )

    def test_roadmap_seed_with_none_predecessor_ok(self):
        fm = _valid_handoff(kind='roadmap-seed', predecessor='none')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'predecessor' for e in errors)

    def test_roadmap_seed_with_null_predecessor_ok(self):
        fm = _valid_handoff(kind='roadmap-seed', predecessor=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'predecessor' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — origin-axis (Rules C2-1 through C2-5)
#
# Port of coordinator-claude coordinator/bin/lib/schema.js:1283-1553 origin_* cross-field
# rules; test vectors mirrored from coordinator-claude's schema.test.js (commit 70f16d4/0eef1a5).
# ---------------------------------------------------------------------------

def _base_spinoff_with_origin(**overrides) -> dict:
    """Base valid spinoff handoff carrying well-formed origin_* fields."""
    base = {
        'title': 'Spinoff with origin provenance',
        'created': '2026-07-07',
        'branch': 'work/machine-a/2026-07-07',
        'status': 'open',
        'predecessor': 'none',
        'kind': 'spinoff',
        'category': 'roadmap',
        'summary': 'Spinoff baton with well-formed origin_* provenance fields',
        'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
        'origin_handoff': 'state/handoffs/2026-07-06-parent-baton.md',
        'origin_plan_id': 'pln-structured-originating-session-8b505c',
        'origin_goal_id': ['goal-shipping-velocity'],
    }
    base.update(overrides)
    return base


class TestOriginAxisC21WellFormedness:
    """Rule C2-1: kind-prefixed id well-formedness."""

    def test_well_formed_origin_fields_on_spinoff_pass(self):
        fm = _base_spinoff_with_origin()
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_origin_fields_absent_are_noop(self):
        fm = _valid_handoff(kind='session-handoff')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_all_origin_fields_null_pass(self):
        """backfill=null policy: explicit nulls are valid."""
        fm = _base_spinoff_with_origin(
            origin_session=None, origin_handoff=None, origin_plan_id=None, origin_goal_id=None,
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_origin_plan_id_without_prefix_rejected(self):
        fm = _base_spinoff_with_origin(origin_plan_id='plan-some-slug')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_plan_id'), None)
        assert err is not None, f'Errors: {errors}'
        assert 'C2-1' in err['error']

    def test_origin_handoff_not_state_handoffs_path_rejected(self):
        fm = _base_spinoff_with_origin(origin_handoff='archive/handoffs/2026-07-06-old.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_handoff'), None)
        assert err is not None, f'Errors: {errors}'
        assert 'C2-1' in err['error']

    def test_origin_session_empty_string_rejected(self):
        fm = _base_spinoff_with_origin(origin_session='   ')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_session'), None)
        assert err is not None, f'Errors: {errors}'
        assert 'C2-1' in err['error']

    def test_origin_goal_id_entry_without_goal_prefix_rejected(self):
        fm = _base_spinoff_with_origin(origin_goal_id=['gol-legibility', 'goal-shipping-velocity'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_goal_id'), None)
        assert err is not None, f'Errors: {errors}'
        assert 'C2-1' in err['error']

    def test_origin_goal_id_entry_with_old_g_prefix_rejected(self):
        fm = _base_spinoff_with_origin(origin_goal_id=['g-legibility'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'origin_goal_id' for e in errors)

    def test_origin_goal_id_multiple_valid_entries_pass(self):
        fm = _base_spinoff_with_origin(
            origin_goal_id=['goal-shipping-velocity', 'goal-legibility-system'],
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'origin_goal_id' for e in errors)

    def test_origin_goal_id_empty_array_passes(self):
        fm = _base_spinoff_with_origin(origin_goal_id=[])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'origin_goal_id' for e in errors)


class TestOriginAxisC22Cardinality:
    """Rule C2-2: explicit per-kind cardinality."""

    def test_origin_session_array_rejected(self):
        fm = _base_spinoff_with_origin(origin_session=['a1b2c3d4-e5f6-7890-abcd-ef1234567890'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_session' and 'C2-2' in e['error']), None)
        assert err is not None, f'Errors: {errors}'
        assert 'scalar string (not an array)' in err['error']

    def test_origin_handoff_array_rejected(self):
        fm = _base_spinoff_with_origin(origin_handoff=['state/handoffs/2026-07-06-parent-baton.md'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_handoff' and 'C2-2' in e['error']), None)
        assert err is not None, f'Errors: {errors}'
        assert 'scalar string (not an array)' in err['error']

    def test_origin_plan_id_array_rejected(self):
        fm = _base_spinoff_with_origin(origin_plan_id=['pln-structured-originating-session-8b505c'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_plan_id' and 'C2-2' in e['error']), None)
        assert err is not None, f'Errors: {errors}'
        assert 'scalar string (not an array)' in err['error']

    def test_origin_goal_id_bare_scalar_rejected(self):
        fm = _base_spinoff_with_origin(origin_goal_id='goal-shipping-velocity')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_goal_id' and 'C2-2' in e['error']), None)
        assert err is not None, f'Errors: {errors}'
        assert 'not a bare scalar' in err['error']

    def test_origin_session_as_string_passes(self):
        fm = _base_spinoff_with_origin(origin_session='a1b2c3d4-e5f6-7890-abcd-ef1234567890')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'origin_session' for e in errors)

    def test_origin_goal_id_as_array_passes(self):
        fm = _base_spinoff_with_origin(origin_goal_id=['goal-shipping-velocity'])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'origin_goal_id' for e in errors)


class TestOriginAxisC23SelfReference:
    """Rule C2-3: direct self-reference rejection (best-effort via _filePath sentinel)."""

    def test_origin_handoff_equals_own_path_rejected(self):
        own_path = 'state/handoffs/2026-07-07-my-spinoff.md'
        fm = _base_spinoff_with_origin(origin_handoff=own_path, _filePath=own_path)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_handoff'), None)
        assert err is not None, f'Errors: {errors}'
        assert "equals the record's own path" in err['error']
        assert 'C2-3' in err['error']

    def test_origin_handoff_different_from_own_path_passes(self):
        fm = _base_spinoff_with_origin(
            origin_handoff='state/handoffs/2026-07-06-parent-baton.md',
            _filePath='state/handoffs/2026-07-07-my-spinoff.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'origin_handoff' for e in errors)

    def test_origin_handoff_present_no_file_path_sentinel_is_noop(self):
        """Without _filePath, the self-reference check cannot fire (partial acyclicity)."""
        fm = _base_spinoff_with_origin(origin_handoff='state/handoffs/2026-07-07-same-as-own.md')
        fm.pop('_filePath', None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    # Review: code-reviewer — F3: durability test for the _filePath/additionalProperties
    # interaction. The real handoff.schema.json does NOT set additionalProperties: false,
    # so the C2-3 self-reference sentinel (fm['_filePath']) is never exercised under a
    # schema that COULD reject it as an undeclared key. This test uses an inline minimal
    # schema (mirrors the JS oracle's handoffJsonSchema/minimalSchema fixture pattern) that
    # DOES set additionalProperties: false.
    #
    # Re-verification during integration found the executor's "verified empirically" claim
    # does NOT hold under this condition: '_filePath' is passed straight into
    # _validate_json_schema_node(fm_dict, ...) (schema_validate.py:1231) alongside every
    # other frontmatter key — it is never stripped as a validator-internal sentinel before
    # the shape gate runs. Against a schema that sets additionalProperties: false and does
    # NOT declare '_filePath' in properties, this DOES currently produce a spurious
    # "additional property \"_filePath\" not allowed" error. The claim is safe ONLY because
    # the real handoff.schema.json has no additionalProperties gate at all (true by
    # construction, not by any '_filePath' special-casing) — exactly Finding 3's caveat.
    # This test pins the CURRENT (fragile) behavior as a regression guard: if a future edit
    # adds additionalProperties: false to the real schema without also declaring '_filePath'
    # in properties, every handoff record's C2-3 self-reference check will start failing
    # shape validation, and this test will already be red as an early warning.
    def test_file_path_sentinel_trips_additional_properties_false_schema(self, tmp_path):
        minimal_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'x-schema-name': 'handoff',
            'type': 'object',
            'required': ['title'],
            'properties': {
                'title': {'type': 'string'},
                'origin_handoff': {'type': 'string'},
            },
            'additionalProperties': False,
        }
        schema_path = tmp_path / 'minimal-additional-properties-false.schema.json'
        schema_path.write_text(json.dumps(minimal_schema))

        own_path = 'state/handoffs/2026-07-07-my-spinoff.md'
        fm = {
            'title': 'Minimal record for additionalProperties interaction test',
            'origin_handoff': 'state/handoffs/2026-07-06-parent-baton.md',
            '_filePath': own_path,
        }
        errors = validate_frontmatter(fm, schema_path)
        err = next(
            (e for e in errors if e['field'] == '_filePath' and 'additional property' in e['error']),
            None,
        )
        assert err is not None, (
            f'Expected _filePath to trip additionalProperties:false (pinning current '
            f'fragile behavior — see Finding 3). Errors: {errors}'
        )


class TestOriginAxisC24PredecessorNoneInvariant:
    """Rule C2-4: predecessor:none invariant preserved even when origin_* present."""

    def test_spinoff_with_origin_and_predecessor_none_passes(self):
        fm = _base_spinoff_with_origin(predecessor='none')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_spinoff_with_origin_and_predecessor_null_passes(self):
        fm = _base_spinoff_with_origin(predecessor=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_spinoff_with_origin_and_non_none_predecessor_rejected(self):
        fm = _base_spinoff_with_origin(predecessor='state/handoffs/2026-07-06-prior.md')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        c24_err = next(
            (e for e in errors if e['field'] == 'predecessor' and 'C2-4' in e['error']), None
        )
        assert c24_err is not None, f'Errors: {errors}'
        assert 'predecessor:none invariant' in c24_err['error']

    def test_goal_seed_with_origin_and_predecessor_none_passes(self):
        fm = {
            'title': 'Goal spinoff with origin provenance',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'goal-seed',
            'category': 'roadmap',
            'summary': 'Goal-scoped spinoff with origin provenance',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
            'origin_goal_id': ['goal-shipping-velocity'],
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_goal_seed_legacy_alias_with_origin_and_predecessor_none_passes_at_cross_field_layer(self):
        """The retired `spinoff-goal` spelling is cross-field-clean under the
        origin-axis C2-4 rule — same as goal-seed — and now also validates
        clean at the `kind` enum layer: `_tolerate_handoff_kind_aliases`
        de-aliases a still-live D1 pre-rename spelling via `canonical_kind()`
        before the enum check's error is kept, closing the enum-level gap this
        test previously documented as open (see executor report for
        coordinatorexecutor-5bb50c81 dispatch, and the fix that closed it)."""
        fm = {
            'title': 'Goal spinoff with origin provenance',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'spinoff-goal',
            'category': 'roadmap',
            'summary': 'Goal-scoped spinoff with origin provenance',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
            'origin_goal_id': ['goal-shipping-velocity'],
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_roadmap_seed_with_origin_and_predecessor_none_passes(self):
        fm = {
            'title': 'Roadmap-creator spinoff with origin provenance',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'roadmap-seed',
            'category': 'roadmap',
            'summary': 'Roadmap-creator spinoff with origin provenance',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_roadmap_seed_legacy_alias_with_origin_and_predecessor_none_passes_at_cross_field_layer(self):
        """The retired `spinoff-roadmap-creator` spelling is cross-field-clean
        under C2-4 and now also validates clean at the `kind` enum layer — see
        the goal-seed sibling test above for the full explanation."""
        fm = {
            'title': 'Roadmap-creator spinoff with origin provenance',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'spinoff-roadmap-creator',
            'category': 'roadmap',
            'summary': 'Roadmap-creator spinoff with origin provenance',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_session_handoff_with_origin_fields_does_not_trigger_c24(self):
        fm = _valid_handoff(
            kind='session-handoff',
            origin_session='a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            origin_plan_id='pln-some-plan-id',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_spinoff_with_no_origin_fields_does_not_trigger_c24(self):
        fm = {
            'title': 'Ordinary spinoff without origin provenance',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'spinoff',
            'category': 'roadmap',
            'summary': 'Spinoff without origin_* fields — backfill=null case',
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'


class TestOriginAxisC25ForkedFromEquality:
    """Rule C2-5: forked_from / origin_handoff never-silently-disagree invariant."""

    def test_forked_from_and_origin_handoff_equal_passes(self):
        fm = _base_spinoff_with_origin(
            forked_from='state/handoffs/2026-07-06-parent-baton.md',
            origin_handoff='state/handoffs/2026-07-06-parent-baton.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_forked_from_backslashes_origin_handoff_forward_slashes_same_file_passes(self):
        """Normalisation fires: backslash vs forward-slash naming the same file is not divergence."""
        fm = _base_spinoff_with_origin(
            forked_from='state\\handoffs\\2026-07-06-parent-baton.md',
            origin_handoff='state/handoffs/2026-07-06-parent-baton.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_forked_from_and_origin_handoff_disagree_rejected(self):
        fm = _base_spinoff_with_origin(
            forked_from='state/handoffs/2026-07-06-parent-baton.md',
            origin_handoff='state/handoffs/2026-07-05-different-baton.md',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == 'origin_handoff'), None)
        assert err is not None, f'Errors: {errors}'
        assert 'lineage-integrity divergence' in err['error']
        assert 'C2-5' in err['error']

    def test_origin_handoff_set_forked_from_absent_on_goal_seed_passes(self):
        """No presence-coupling: forked_from is validation-illegal on goal-seed, but
        origin_handoff alone is legal and must not trip C2-5."""
        fm = {
            'title': 'Goal spinoff with origin_handoff but no forked_from',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'goal-seed',
            'category': 'roadmap',
            'summary': 'Goal-scoped spinoff — forked_from is validation-illegal on this kind',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_handoff': 'state/handoffs/2026-07-06-parent-baton.md',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
            'origin_goal_id': ['goal-shipping-velocity'],
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_origin_handoff_set_forked_from_absent_on_goal_seed_legacy_alias_passes_at_cross_field_layer(self):
        """The retired `spinoff-goal` spelling is cross-field-clean under C2-5
        (no presence-coupling error) and now also validates clean at the
        `kind` enum layer — same de-aliasing as the C2-4 legacy-alias tests
        above."""
        fm = {
            'title': 'Goal spinoff with origin_handoff but no forked_from',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'spinoff-goal',
            'category': 'roadmap',
            'summary': 'Goal-scoped spinoff — forked_from is validation-illegal on this kind',
            'origin_session': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'origin_handoff': 'state/handoffs/2026-07-06-parent-baton.md',
            'origin_plan_id': 'pln-structured-originating-session-8b505c',
            'origin_goal_id': ['goal-shipping-velocity'],
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'

    def test_forked_from_set_origin_handoff_absent_on_spinoff_passes(self):
        fm = {
            'title': 'Spinoff with forked_from but no origin_handoff',
            'created': '2026-07-07',
            'branch': 'work/machine-a/2026-07-07',
            'status': 'open',
            'predecessor': 'none',
            'kind': 'spinoff',
            'category': 'roadmap',
            'summary': 'Spinoff with forked_from but no origin_handoff',
            'forked_from': 'state/handoffs/2026-07-06-parent-baton.md',
        }
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == [], f'Errors: {errors}'


# ---------------------------------------------------------------------------
# Cross-field rules — owner-axis scalar (mcollab-03/oaxis-03 merge, C2/C4)
# ---------------------------------------------------------------------------

_OWNERSHIP_AXIS_FIELDS = (
    'owner',
    'assignee',
    'claimant',
    'consumed_by_human',
    'to_human',
    'created_by_human',
)


class TestIsUnowned:
    """the Data Science Reviewer F1: is_unowned(v) treats absent/None/whitespace-only as equivalent."""

    def test_none_is_unowned(self):
        assert is_unowned(None) is True

    def test_empty_string_is_unowned(self):
        assert is_unowned('') is True

    def test_whitespace_only_string_is_unowned(self):
        assert is_unowned('   ') is True

    def test_real_slug_is_not_unowned(self):
        assert is_unowned('em-machine-a') is False


class TestOwnerAxisScalar:
    """Owner-axis cross-field rule: reject empty/whitespace-only ownership strings.

    Three-case pattern (needs / ok / boundary) per axis field, mirroring
    TestClaimedByRequired's shape.
    """

    def test_absent_owner_axes_pass(self):
        """Boundary: every ownership axis absent (the N=1 solo-inert default) is valid."""
        fm = _valid_handoff()
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] in _OWNERSHIP_AXIS_FIELDS for e in errors)

    def test_explicit_null_owner_axes_pass(self):
        fm = _valid_handoff(**{field: None for field in _OWNERSHIP_AXIS_FIELDS})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] in _OWNERSHIP_AXIS_FIELDS for e in errors)

    def test_explicit_owner_slug_passes(self):
        """ok: a real slug on every axis is valid — the explicit-owner case."""
        fm = _valid_handoff(**{field: 'em-machine-a' for field in _OWNERSHIP_AXIS_FIELDS})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] in _OWNERSHIP_AXIS_FIELDS for e in errors)

    @pytest.mark.parametrize('field', _OWNERSHIP_AXIS_FIELDS)
    def test_empty_string_owner_axis_rejected(self, field):
        """needs: an empty-string ownership axis is the malformed in-between the rule exists
        to catch — reject-with-hint, not silent normalization."""
        fm = _valid_handoff(**{field: ''})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == field), None)
        assert err is not None, f'Errors: {errors}'
        assert 'empty string' in err['error']
        assert 'is_unowned' in err['error']

    @pytest.mark.parametrize('field', _OWNERSHIP_AXIS_FIELDS)
    def test_whitespace_only_owner_axis_rejected(self, field):
        """needs: whitespace-only is equally malformed under is_unowned's .trim() semantics.

        Review: code-reviewer — F4: the message must name the whitespace-only
        case distinctly (not just reuse "empty string" wording), since a
        visually-non-empty value like '   ' being told it's "an empty string"
        is confusing to the operator who wrote it.
        """
        fm = _valid_handoff(**{field: '   '})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        err = next((e for e in errors if e['field'] == field), None)
        assert err is not None, f'Errors: {errors}'
        assert 'whitespace-only' in err['error']


# ---------------------------------------------------------------------------
# Cross-field rules — additional_predecessors integrity
# ---------------------------------------------------------------------------

class TestAdditionalPredecessors:
    def test_no_duplicate_entries(self):
        fm = _valid_handoff(
            additional_predecessors=[
                'state/handoffs/a.md',
                'state/handoffs/a.md',
            ]
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'additional_predecessors' and 'duplicate' in e['error'] for e in errors)

    def test_no_entry_duplicating_primary_predecessor(self):
        fm = _valid_handoff(
            predecessor='state/handoffs/primary.md',
            additional_predecessors=['state/handoffs/primary.md'],
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'additional_predecessors' and 'primary' in e['error'] for e in errors)

    def test_valid_additional_predecessors(self):
        fm = _valid_handoff(
            predecessor='state/handoffs/a.md',
            additional_predecessors=['state/handoffs/b.md', 'state/handoffs/c.md'],
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'additional_predecessors' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rules — deliverable_id prefix / initiative non-empty
# ---------------------------------------------------------------------------

class TestDeliverableSpineFields:
    def test_deliverable_id_without_prefix_fails(self):
        fm = _valid_handoff(deliverable_id='my-deliverable')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'deliverable_id' and 'dlv-' in e['error'] for e in errors)

    def test_deliverable_id_with_prefix_ok(self):
        fm = _valid_handoff(deliverable_id='dlv-foo-abc123')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'deliverable_id' for e in errors)

    def test_deliverable_id_null_ok(self):
        fm = _valid_handoff(deliverable_id=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'deliverable_id' for e in errors)

    def test_initiative_empty_string_fails(self):
        fm = _valid_handoff(initiative='')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'initiative' and 'non-empty' in e['error'] for e in errors)

    def test_initiative_valid_string_ok(self):
        fm = _valid_handoff(initiative='example-fleet-pro-launch')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'initiative' for e in errors)

    def test_initiative_null_ok(self):
        fm = _valid_handoff(initiative=None)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'initiative' for e in errors)


# ---------------------------------------------------------------------------
# sizing_object — FK to the sizing-object that routed a plan through
# coordinator:sizing. Optional/nullable; string arm constrained to
# state/sizings/<id>.yaml. Spec backlink:
# docs/plans/2026-08-06-plan-sizing-citation-gate.md § C1.
# ---------------------------------------------------------------------------

class TestSizingObjectField:
    def test_absent_ok(self):
        """The common case pre-convention: no sizing_object key at all."""
        errors = validate_frontmatter(_valid_plan(), _PLAN_SCHEMA)
        assert not any(e['field'] == 'sizing_object' for e in errors)

    def test_null_ok(self):
        fm = _valid_plan(sizing_object=None)
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert not any(e['field'] == 'sizing_object' for e in errors)

    def test_valid_path_ok(self):
        fm = _valid_plan(sizing_object='state/sizings/2026-08-06-example.yaml')
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert not any(e['field'] == 'sizing_object' for e in errors)

    def test_path_outside_state_sizings_rejected(self):
        fm = _valid_plan(sizing_object='state/other/2026-08-06-example.yaml')
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert any(e['field'] == 'sizing_object' for e in errors)

    def test_non_yaml_suffix_rejected(self):
        fm = _valid_plan(sizing_object='state/sizings/2026-08-06-example.yml')
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert any(e['field'] == 'sizing_object' for e in errors)

    def test_bare_filename_rejected(self):
        fm = _valid_plan(sizing_object='2026-08-06-example.yaml')
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert any(e['field'] == 'sizing_object' for e in errors)


# ---------------------------------------------------------------------------
# Corpus lint: every docs/plans/*.md still validates against the bumped
# plan.schema.json. 315 of 332 on-disk plans predate the sizing_object
# convention and carry no such key at all — this is the AC1 "all 332
# existing plans still validate" assertion. Spec backlink:
# docs/plans/2026-08-06-plan-sizing-citation-gate.md § C1.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# sizing-object — deliverable_id/plan/status:shipped (1.7.0 -> 1.8.0 vendor
# bump, commit 90bd3cbf0). Regression for
# state/bug-backlog/2026-08-10-frontmatter-schema-guard-rejects-deliver-7a9d9e004454.yaml:
# the commit message claimed the vendor added `deliverable_id`, but the
# byte content it actually committed still carried the pre-bump 1.7.0 shape
# with no `deliverable_id`/`plan`/`shipped` — the fix existed only in an
# uncommitted working-tree edit. This asserts the ON-DISK schema (whatever
# is about to be committed) accepts the fields its own bump note claims.
# ---------------------------------------------------------------------------

class TestSizingObjectDeliverableSpineFields:
    """A minimal sizing-object built from required-fields-only, with
    deliverable_id/plan/status:shipped layered on. Fails against the
    pre-bump (1.7.0) schema shape and must pass against the current one.

    Landed parked under `pending_fix` because commit `90bd3cbf0` committed
    1.7.0 bytes under a 1.8.0-vendor message and the schema file was then
    held by a live peer path-touch claim (see
    `state/bug-backlog/2026-08-10-frontmatter-schema-guard-rejects-deliver-7a9d9e004454.yaml`).
    The real 1.8.0 bytes landed in `6d79c48cf`, so the park's own removal
    condition is discharged and this class is a live gate again."""

    @staticmethod
    def _minimal_sizing_object(**overrides):
        fm = {
            'schema': 'sizing-object',
            'intent': 'Example PM ask, verbatim.',
            'estimate': {'tshirt': 'M', 'provisional': True},
            'route': 'plan',
            'detents': [],
            'fork': None,
            'xl_exit': None,
            'status': 'routed',
            'premise': {'provenance': 'unrecorded'},
        }
        fm.update(overrides)
        return fm

    def test_deliverable_id_valid_pattern_ok(self):
        fm = self._minimal_sizing_object(deliverable_id='dlv-example-abc123')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'deliverable_id' for e in errors), errors

    def test_deliverable_id_null_ok(self):
        fm = self._minimal_sizing_object(deliverable_id=None)
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'deliverable_id' for e in errors), errors

    def test_deliverable_id_absent_ok(self):
        fm = self._minimal_sizing_object()
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'deliverable_id' for e in errors), errors

    def test_plan_fk_valid_path_ok(self):
        fm = self._minimal_sizing_object(plan='docs/plans/2026-08-10-example.md')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'plan' for e in errors), errors

    def test_status_shipped_ok(self):
        fm = self._minimal_sizing_object(status='shipped')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'status' for e in errors), errors

    def test_status_declined_ok(self):
        """2026-08-10 (docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md
        § C1): `declined` — routed, then the spend was refused — is a valid
        terminal status value alongside `shipped`/`superseded`."""
        fm = self._minimal_sizing_object(status='declined')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e['field'] == 'status' for e in errors), errors

    def test_status_invalid_value_rejected(self):
        """Regression guard for the AC1 addition: the enum must still reject
        an arbitrary string — declined is a member, not a widening to any string."""
        fm = self._minimal_sizing_object(status='declined-forever')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e['field'] == 'status' for e in errors), errors

    def test_reported_file_validates_clean(self):
        """The exact repro from the bug entry: a real on-disk sizing-object
        carrying deliverable_id must validate with zero errors."""
        repo_root = Path(__file__).resolve().parents[3]
        target = repo_root / 'state' / 'sizings' / '2026-08-10-are-two-thirds-of-healthy-repo-sessions.yaml'
        if not target.exists():
            pytest.skip(f'{target} not present on this checkout')
        content = target.read_text(encoding='utf-8')
        fm = yaml.safe_load(content)
        assert fm is not None
        assert fm.get('deliverable_id')
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not errors, errors

    def test_x_schema_version_at_least_1_8_0(self):
        # Review: coordinator:code-reviewer c841277a — coordinator-claude's cross-repo
        # parity gate compares shapes ONLY when both sides' x-schema-version
        # are equal; an unequal-version divergence compares nothing and is
        # silent, which is how the deliverable-spine defect went unnoticed.
        # Pin this repo's own floor here since no dedicated version-parity
        # test exists in this repo (test_offerable_schema_vendoring_parity.py
        # checks vendoring presence, not this schema's version floor).
        schema = json.loads(_SIZING_OBJECT_SCHEMA.read_text(encoding='utf-8'))
        version = tuple(int(p) for p in schema['x-schema-version'].split('.'))
        assert version >= (1, 8, 0), schema['x-schema-version']


class TestPlanCorpusValidatesAgainstBumpedSchema:
    """No-regression differential over the ENTIRE docs/plans/*.md corpus
    (plans and review sidecars alike -- sidecar-detection heuristics are
    documented non-exhaustive and irrelevant to what this test asserts):
    every file that validated against the pre-bump schema (git HEAD) still
    validates against the bumped one, and nothing newly fails. This is the
    precise form of AC1's "All 332 existing plans still validate" -- a raw
    validate-and-count against the bumped schema alone conflates pre-existing,
    out-of-scope corpus defects (sidecars missing required fields, one plan
    with a stale `status: shipped` not in the schema enum) with regressions
    this bump could introduce. Comparing bumped vs. pre-bump isolates exactly
    the latter.
    """

    def test_no_regression_vs_pre_bump_schema(self):
        repo_root = Path(__file__).resolve().parents[3]
        plans_dir = repo_root / 'docs' / 'plans'
        plan_paths = sorted(plans_dir.glob('*.md'))
        assert len(plan_paths) > 0, f'No plans found under {plans_dir}'

        baseline_schema = json.loads(
            subprocess.run(
                ['git', 'show', 'HEAD:coordinator_core/frontmatter/schemas/plan.schema.json'],
                cwd=repo_root, check=True, capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            ).stdout
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline_schema_path = Path(tmp_dir) / 'plan.schema.pre-bump.json'
            baseline_schema_path.write_text(json.dumps(baseline_schema), encoding='utf-8')
            regressions: list[str] = []
            validated_bumped = 0
            for path in plan_paths:
                content = path.read_text(encoding='utf-8')
                fm = parse_frontmatter(content)['frontmatter']
                if fm is None:
                    continue
                errors_before = validate_frontmatter(fm, baseline_schema_path)
                errors_after = validate_frontmatter(fm, _PLAN_SCHEMA)
                if errors_after:
                    if not errors_before:
                        regressions.append(f'{path.name}: newly fails: {errors_after}')
                else:
                    validated_bumped += 1

            assert not regressions, (
                f'{len(regressions)} file(s) validated before the sizing_object bump '
                f'and now fail:\n' + '\n'.join(regressions)
            )
            # Sanity: the bump should not have caused everything to silently no-op.
            assert validated_bumped > 0

    def test_sizing_object_bearing_plans_validate(self):
        """The 17 plans the memo found carrying `sizing_object:` as an
        unvalidated extension key now validate under the declared field."""
        repo_root = Path(__file__).resolve().parents[3]
        plans_dir = repo_root / 'docs' / 'plans'
        checked = 0
        for path in sorted(plans_dir.glob('*.md')):
            content = path.read_text(encoding='utf-8')
            fm = parse_frontmatter(content)['frontmatter']
            if fm is None or 'sizing_object' not in fm:
                continue
            checked += 1
            errors = validate_frontmatter(fm, _PLAN_SCHEMA)
            assert not any(e['field'] == 'sizing_object' for e in errors), (
                f'{path.name}: sizing_object failed to validate: {errors}'
            )
        assert checked >= 15, (
            f'Only found {checked} on-disk plans carrying sizing_object -- '
            'expected roughly the 17 the memo surveyed.'
        )


# ---------------------------------------------------------------------------
# Cross-field rule — carried_items shape (the schema half of the disposition
# gate; the ceremony-time counterpart lives in
# coordinator_core.ops.handoff_carry_gate, not here). Carry DEPTH is not
# validated on either side — carries are indefinite (DR-268).
# ---------------------------------------------------------------------------

class TestCarriedItemsShape:
    # handoff.schema.json 7.0.0 (DR-278) dropped `carry_count` from both
    # `carried_items[].required` and `.properties` — it is no longer part of
    # the schema at all. Fixtures below that still carry a `carry_count` key
    # keep it only as inert legacy data (see test_legacy_carry_count_is_inert):
    # `carried_items[]` has no `additionalProperties: false`, so its presence
    # or absence is schema-neutral. No fixture here relies on it being present.
    def test_absent_carried_items_ok(self):
        errors = validate_frontmatter(_valid_handoff(), _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'carried_items' for e in errors)

    def test_valid_carried_entry_ok(self):
        fm = _valid_handoff(carried_items=[
            {
                'carry_id': 'cf-windows-validation-3f2a1c', 'description': 'x',
                'carry_count': 1, 'disposition': 'carried',
            },
        ])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'carried_items' for e in errors)

    def test_legacy_carry_count_is_inert(self):
        """A carry_count left behind in an older handoff is neither required
        nor rejected — the field is gone from the schema, not forbidden."""
        fm = _valid_handoff(carried_items=[
            {'carry_id': 'cf-x-111111', 'description': 'x', 'carry_count': 9, 'disposition': 'carried'},
        ])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'carried_items' for e in errors)

    def test_terminal_disposition_without_detail_fails(self):
        fm = _valid_handoff(carried_items=[
            {'carry_id': 'cf-x-111111', 'description': 'x', 'carry_count': 1, 'disposition': 'closed'},
        ])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'carried_items' and 'disposition_detail' in e['error'] for e in errors)

    def test_terminal_disposition_with_detail_ok(self):
        fm = _valid_handoff(carried_items=[
            {
                'carry_id': 'cf-x-111111', 'description': 'x', 'carry_count': 1,
                'disposition': 'blocked', 'disposition_detail': 'needs a reachable Windows host',
            },
        ])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'carried_items' for e in errors)

    def test_carried_items_unaffected_by_plan_tasks_case_against_gate(self):
        """`case_against` is a plan-tasks-only leg, homed in
        `_cf_plan_tasks_disposition_shape` — NOT in the shared
        `_cf_disposition_shape` both callers reuse. A carried_items entry
        with no `case_against` key at all (the field doesn't exist on this
        schema) must validate clean even for a closed/terminal disposition
        with no PM-approval analogue."""
        fm = _valid_handoff(carried_items=[
            {
                'carry_id': 'cf-x-222222', 'description': 'x', 'carry_count': 1,
                'disposition': 'closed', 'disposition_detail': 'done, no case_against anywhere',
            },
        ])
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'carried_items' for e in errors)
        assert not any(e['field'] == 'case_against' for e in errors)

    def test_cf_carried_items_shape_direct_no_carry_count_no_error(self):
        """Direct unit test of `_cf_carried_items_shape` — the cross-field rule
        itself, called directly rather than through `validate_frontmatter` —
        proving an entry with NO `carry_count` returns no error from this rule.
        This is the behavior DR-268 introduced (carry depth is never
        validated); calling the cross-field rule directly keeps this test
        independent of the base-schema `required` check regardless of what
        the vendored handoff.schema.json currently requires."""
        fm = {'carried_items': [
            {'carry_id': 'cf-x-333333', 'description': 'x', 'disposition': 'carried'},
        ]}
        assert _cf_carried_items_shape(fm) is None

    def test_non_list_carried_items_fails(self):
        fm = _valid_handoff(carried_items='not-a-list')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'carried_items' for e in errors)


# ---------------------------------------------------------------------------
# Cross-field rule — plan-tasks spine row disposition (C2, D2/D3/D4's
# hard-failing layer). plan-tasks.schema.json's x-schema-name is a PER-ROW
# schema, so validate_frontmatter(row, _PLAN_TASKS_SCHEMA) exercises exactly
# one spine row per call — mirrors TestCarriedItemsShape's case shapes.
# ---------------------------------------------------------------------------

def _valid_plan_task_row(**overrides) -> dict:
    """Minimal valid plan-tasks row (base-schema-required fields only)."""
    base = {
        'id': 'C1',
        'title': 'Test row',
        'change_kind': 'code-edit',
        'surface': 'some/path.py',
    }
    base.update(overrides)
    return base


class TestPlanTasksDispositionShape:
    def test_default_open_row_ok(self):
        errors = validate_frontmatter(_valid_plan_task_row(), _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] in ('disposition', 'disposition_ref', 'pm_approved') for e in errors)

    def test_coded_with_valid_sha_and_detail_ok(self):
        row = _valid_plan_task_row(
            disposition='coded',
            disposition_ref='a1b2c3d',
            disposition_detail='shipped in a1b2c3d',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] in ('disposition', 'disposition_ref', 'pm_approved') for e in errors)

    def test_coded_with_no_detail_ok(self):
        """Regression: `close_out_and_stamp.py`'s `_auto_resolve_committed_open_rows`
        writes `disposition: coded` + `disposition_ref` and NO
        `disposition_detail` — plan-tasks.schema.json's `$comment` on the
        spun_off/backlogged conditional is explicit that `coded` is evidence
        of work done, not a scope decision, and is therefore exempt from the
        detail requirement. This must be schema-valid with no detail at all."""
        row = _valid_plan_task_row(
            disposition='coded',
            disposition_ref='304ecc26',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'disposition' and 'disposition_detail' in e['error'] for e in errors)

    def test_coded_requires_no_pm_approved(self):
        """D3: coded is evidence of work, not a scope decision — no PM gate."""
        row = _valid_plan_task_row(
            disposition='coded',
            disposition_ref='a1b2c3d',
            disposition_detail='shipped in a1b2c3d',
            pm_approved=False,
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'pm_approved' for e in errors)

    def test_coded_bad_sha_shape_rejected(self):
        row = _valid_plan_task_row(
            disposition='coded',
            disposition_ref='not-a-sha!,also-a-range..x',
            disposition_detail='bogus ref',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition_ref' and 'hex SHA' in e['error'] for e in errors)

    def test_coded_missing_ref_rejected(self):
        row = _valid_plan_task_row(disposition='coded', disposition_detail='no ref given')
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition' and 'disposition_ref' in e['error'] for e in errors)

    def test_spun_off_no_longer_requires_pm_approved(self):
        """coordinator-claude's 2026-08-05 ruling relaxed spun_off's pm_approved gate: moving
        a row to another plan drops no work, so there is no scope cut for the
        PM to ratify — the EM self-issues it now."""
        row = _valid_plan_task_row(
            disposition='spun_off',
            disposition_ref='docs/plans/2026-07-27-successor.md',
            disposition_detail='forked to successor plan',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'pm_approved' for e in errors)

    def test_spun_off_with_pm_approved_ok(self):
        row = _valid_plan_task_row(
            disposition='spun_off',
            disposition_ref='docs/plans/2026-07-27-successor.md',
            disposition_detail='forked to successor plan',
            pm_approved=True,
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] in ('disposition', 'disposition_ref', 'pm_approved') for e in errors)

    def test_backlogged_ref_rejects_comma_list(self):
        row = _valid_plan_task_row(
            disposition='backlogged',
            disposition_ref='state/improvement-queue/a.yaml, state/improvement-queue/b.yaml',
            disposition_detail='deferred to backlog',
            pm_approved=True,
            case_against='not worth the cycles right now',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition_ref' and 'repo-relative path' in e['error'] for e in errors)

    def test_backlogged_ref_rejects_range(self):
        row = _valid_plan_task_row(
            disposition='backlogged',
            disposition_ref='state/improvement-queue/a..b.yaml',
            disposition_detail='deferred to backlog',
            pm_approved=True,
            case_against='not worth the cycles right now',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition_ref' and 'repo-relative path' in e['error'] for e in errors)

    def test_wont_do_with_detail_and_no_ref_and_pm_approved_ok(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
            pm_approved=True,
            case_against='out of scope for this plan, not worth the churn',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] in ('disposition', 'disposition_ref', 'pm_approved') for e in errors)

    def test_wont_do_requires_pm_approved(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'pm_approved' for e in errors)

    def test_wont_do_carrying_ref_rejected(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
            disposition_ref='docs/plans/should-not-be-here.md',
            pm_approved=True,
            case_against='out of scope for this plan, not worth the churn',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition' and 'disposition_ref' in e['error'] for e in errors)

    def test_non_open_missing_detail_rejected(self):
        row = _valid_plan_task_row(
            disposition='spun_off',
            disposition_ref='docs/plans/successor.md',
            pm_approved=True,
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'disposition' and 'disposition_detail' in e['error'] for e in errors)


class TestPlanTasksCaseAgainstShape:
    """Hard-rejection leg for the cross-repo coordinator-claude memo (2026-08-06, leg 1):
    a closed scope-cut row (backlogged/wont_do) needs a non-empty
    case_against. Homed in `_cf_plan_tasks_disposition_shape`, NOT the
    shared `_cf_disposition_shape` — see that function's docstring for why
    the memo's named locus (`_cf_disposition_shape`) is wrong: it is the
    handoff/plan-tasks common subset and `carried_items` has no
    `case_against` field at all.
    """

    def test_backlogged_absent_case_against_rejected(self):
        row = _valid_plan_task_row(
            disposition='backlogged',
            disposition_detail='deferred to backlog',
            disposition_ref='state/improvement-queue/a.yaml',
            pm_approved=True,
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'case_against' for e in errors)

    def test_backlogged_whitespace_case_against_rejected(self):
        row = _valid_plan_task_row(
            disposition='backlogged',
            disposition_detail='deferred to backlog',
            disposition_ref='state/improvement-queue/a.yaml',
            pm_approved=True,
            case_against='   \n\t  ',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'case_against' for e in errors)

    def test_wont_do_absent_case_against_rejected(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
            pm_approved=True,
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'case_against' for e in errors)

    def test_wont_do_whitespace_case_against_rejected(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
            pm_approved=True,
            case_against='  ',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert any(e['field'] == 'case_against' for e in errors)

    def test_wont_do_non_blank_case_against_ok(self):
        row = _valid_plan_task_row(
            disposition='wont_do',
            disposition_detail='declined — out of scope',
            pm_approved=True,
            case_against='out of scope for this plan, not worth the churn',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'case_against' for e in errors)

    def test_spun_off_never_requires_case_against(self):
        """spun_off is deliberately excluded — nothing leaves the corpus on
        a spinoff, so there is no scope cut to argue against (coordinator-claude
        2026-08-05 ruling)."""
        row = _valid_plan_task_row(
            disposition='spun_off',
            disposition_ref='docs/plans/2026-07-27-successor.md',
            disposition_detail='forked to successor plan',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'case_against' for e in errors)

    def test_coded_never_requires_case_against(self):
        row = _valid_plan_task_row(
            disposition='coded',
            disposition_ref='a1b2c3d',
            disposition_detail='shipped in a1b2c3d',
        )
        errors = validate_frontmatter(row, _PLAN_TASKS_SCHEMA)
        assert not any(e['field'] == 'case_against' for e in errors)

    def test_case_against_gate_survives_governed_suppression(self):
        """governed=True suppresses the pm_approved leg only — case_against
        argues the scope cut itself, independent of which authorization
        mechanism ratified it, so it must still fire."""
        row = {
            'id': 'C2',
            'disposition': 'backlogged',
            'disposition_detail': 'because',
            'disposition_ref': 'docs/plans/x.md',
        }
        error = _cf_plan_tasks_disposition_shape(row, governed=True)
        assert error is not None
        assert error['field'] == 'case_against'


class TestPlanTasksSchemaWithoutPmApprovedRequiredIsNonMutating:
    """Regression guard for the order-dependent-test-pollution class of bug
    (2026-07-29): `_plan_tasks_schema_without_pm_approved_required` is called
    with a shared, module-level schema dict from three call sites
    (`ops/plan_tasks_mutate.py`'s mutate path and both write guards'
    per-row leg), each of which resolves its OWN schema object once and
    reuses it across many rows/tests in the same process. If this
    derivation ever mutated its input in place (or shallow-copied so the
    `allOf` list — or a branch dict inside it — aliased the source), the
    corruption would be invisible to a test run in isolation and would only
    surface as a spooky failure in a DIFFERENT test file that happens to run
    later in the same process and reads the same shared schema object. This
    was the prime suspect investigated for exactly that failure shape; it
    turned out NOT to be the root cause of that particular bug (see
    `TestPlanTasksSpineDeny::test_governed_closed_row_without_pm_approved_passes_schema_layer`
    in `coordinator_core/write_guards/tests/test_validate_frontmatter_schema_deny.py`
    for the actual defect), but the hazard this derivation would create if
    it ever DID mutate in place is real and worth guarding directly rather
    than only indirectly via the write-guard tests.
    """

    def test_source_schema_is_byte_identical_after_derivation(self):
        import copy

        source = copy.deepcopy(_PLAN_TASKS_SCHEMA_DICT)
        before = copy.deepcopy(source)
        _plan_tasks_schema_without_pm_approved_required(source)
        assert source == before, (
            "deriving the governed (pm_approved-not-required) variant must not "
            "mutate the source schema dict passed in — a future in-place edit "
            "here would silently corrupt every other reader sharing this object"
        )

    def test_returned_variant_is_a_distinct_object_not_an_alias(self):
        source = {"allOf": [{"then": {"required": ["pm_approved"]}}]}
        variant = _plan_tasks_schema_without_pm_approved_required(source)
        assert variant is not source
        assert variant["allOf"] is not source["allOf"]
        # The dropped branch must still be present, untouched, on the source.
        assert source["allOf"] == [{"then": {"required": ["pm_approved"]}}]
        assert variant["allOf"] == []

    def test_default_arg_derivation_does_not_mutate_module_level_dict(self):
        """`_PLAN_TASKS_SCHEMA_GOVERNED_DICT` is derived once at import time
        by calling this function with no `schema` argument (defaulting to
        `_PLAN_TASKS_SCHEMA_DICT`). Calling it again here (as a write guard
        or `ops/plan_tasks_mutate.py` would, resolving their own copy) must
        not retroactively corrupt the ALREADY-derived module-level default,
        since callers across the process hold onto that object for the
        lifetime of the interpreter.
        """
        import copy

        from coordinator_core.frontmatter.schema_validate import (
            _PLAN_TASKS_SCHEMA_GOVERNED_DICT,
        )

        before = copy.deepcopy(_PLAN_TASKS_SCHEMA_GOVERNED_DICT)
        _plan_tasks_schema_without_pm_approved_required()
        assert _PLAN_TASKS_SCHEMA_GOVERNED_DICT == before


# ---------------------------------------------------------------------------
# Ordering lint (C3, D5): closed rows sort to the bottom of the spine.
#
# check_plan_tasks_ordering operates on a plan's WHOLE raw source text (not
# a single row's frontmatter dict, unlike TestPlanTasksDispositionShape
# above) — it locates the ```yaml plan-tasks``` fence itself.
# ---------------------------------------------------------------------------

def _plan_source(tasks_yaml: str, *, heading: str = '## Tasks') -> str:
    """Build a minimal plan document with a `## Tasks` section wrapping a
    ```yaml plan-tasks``` fenced block, matching body_blocks.locate_fenced_block's
    default heading/info-string."""
    return (
        "# A plan\n\n"
        f"{heading}\n\n"
        "```yaml plan-tasks\n"
        f"{tasks_yaml}\n"
        "```\n"
    )


class TestPlanTasksOrdering:
    def test_all_open_ok(self):
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "- id: C2\n"
            "  title: two\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_all_closed_ok(self):
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: coded\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: wont_do\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_open_rows_before_closed_rows_ok(self):
        """Valid D5 shape: every open row precedes every closed row."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: coded\n"
            "- id: C3\n"
            "  title: three\n"
            "  disposition: wont_do\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_open_row_after_coded_row_rejected_again(self):
        """Re-tightened at the 2026-07-29 coordinator-claude sub-order ask, one commit
        after this same test asserted the opposite.

        The 2026-07-29 grouping widening moved `coded` from the old
        two-group closed band into `do` alongside `open`, and for one
        commit this test asserted that ordering WITHIN `do` was free — so
        open-after-coded read as valid. Coordinator-claude flagged that as a silent
        regression: under the retired two-group rule `coded` counted as
        closed, so open-after-coded was already rejected, and dropping that
        lint was widening the rule further than the band merge required.
        `_PLAN_TASKS_SUBORDER_BY_DISPOSITION` restores it as a sub-order
        inside `do` alone — `defer` and `ruled_out` stay internally
        unordered, only `do` carries the extra rank. The principle is the
        same one level down: live work reads first, shipped work sinks.
        """
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: coded\n"
            "- id: C2\n"
            "  title: two\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert error['field'] == 'plan-tasks'
        assert "'C2'" in error['error']
        assert "'C1'" in error['error']
        assert 'do' in error['error']

    def test_coded_row_after_open_row_ok(self):
        """The correct order within `do`: open before coded."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: coded\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_do_row_after_defer_row_rejected(self):
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: backlogged\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: coded\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert error['field'] == 'plan-tasks'
        assert "'C2'" in error['error']
        assert "'C1'" in error['error']

    def test_defer_row_after_ruled_out_row_rejected(self):
        """The third group is ordered too — ruled_out sorts below defer."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: wont_do\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: spun_off\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert "'C2'" in error['error']

    def test_three_groups_in_order_ok(self):
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: coded\n"
            "- id: C3\n"
            "  title: three\n"
            "  disposition: spun_off\n"
            "- id: C4\n"
            "  title: four\n"
            "  disposition: backlogged\n"
            "- id: C5\n"
            "  title: five\n"
            "  disposition: wont_do\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_four_way_grouping_order_accepted(self):
        """C3 (2026-08-05): `_PLAN_TASKS_GROUPING_ORDER` is the four-way
        ('do', 'spun_off', 'defer', 'ruled_out') -- spun_off split out of
        'defer' into its own grouping, sorting between live 'do' work and
        the two PM-gated groupings. Same shape as
        `test_three_groups_in_order_ok` above; named explicitly for the C3
        regression so the four-way order has a test that says so on its
        face, not only one inherited from the three-group era."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: coded\n"
            "- id: C3\n"
            "  title: three\n"
            "  disposition: spun_off\n"
            "- id: C4\n"
            "  title: four\n"
            "  disposition: backlogged\n"
            "- id: C5\n"
            "  title: five\n"
            "  disposition: wont_do\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_backlogged_before_spun_off_now_rejected(self):
        """Old (pre-C3) three-way order treated spun_off and backlogged as
        interchangeable members of the same 'defer' grouping -- their
        relative order was free. Post-C3, spun_off has its own grouping
        strictly between 'do' and 'defer' (D5), so a backlogged row before
        a spun_off row is now a violation where it previously was not."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: backlogged\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: spun_off\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert "'C2'" in error['error']

    def test_spun_off_before_backlogged_ok(self):
        """The correct order under the C3 four-way split: spun_off precedes
        backlogged (both differ from the pre-C3 same-grouping tolerance
        exercised in the sibling rejected case above)."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: spun_off\n"
            "- id: C2\n"
            "  title: two\n"
            "  disposition: backlogged\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_open_row_interleaved_among_closed_rows_rejected(self):
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: spun_off\n"
            "- id: C2\n"
            "  title: two\n"
            "- id: C3\n"
            "  title: three\n"
            "  disposition: wont_do\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert "'C2'" in error['error']

    def test_default_open_when_disposition_absent(self):
        """A row with no disposition key defaults to 'open' (D1) for
        ordering purposes too, not just schema validation."""
        source = _plan_source(
            "- id: C1\n"
            "  title: one\n"
            "  disposition: backlogged\n"
            "- id: C2\n"
            "  title: two\n"
        )
        error = check_plan_tasks_ordering(source)
        assert error is not None
        assert "'C2'" in error['error']

    def test_empty_spine_ok(self):
        source = _plan_source("[]")
        assert check_plan_tasks_ordering(source) is None

    def test_absent_spine_returns_none(self):
        """No ```yaml plan-tasks``` fence at all — nothing to check."""
        source = "# A plan\n\nNo tasks here.\n"
        assert check_plan_tasks_ordering(source) is None

    def test_malformed_spine_returns_none(self):
        """Two fences with the same info-string anywhere in the doc ->
        body_blocks MALFORMED — this lint defers to that surface rather than
        raising its own error."""
        source = (
            "# A plan\n\n"
            "## Tasks\n\n"
            "```yaml plan-tasks\n- id: C1\n  title: one\n```\n\n"
            "## Other\n\n"
            "```yaml plan-tasks\n- id: C2\n  title: two\n```\n"
        )
        assert check_plan_tasks_ordering(source) is None

    def test_unparseable_yaml_returns_none(self):
        source = _plan_source("not: valid: yaml: [unterminated")
        assert check_plan_tasks_ordering(source) is None

    def test_non_list_body_returns_none(self):
        source = _plan_source("just_a_string")
        assert check_plan_tasks_ordering(source) is None


# ---------------------------------------------------------------------------
# handoff-archived: no cross-field rules applied
# ---------------------------------------------------------------------------

class TestHandoffArchivedSchema:
    def test_archived_no_cross_field_rules(self):
        """handoff-archived schema applies no cross-field rules (by design)."""
        # This would fail cross-field rules if applied against the handoff schema
        fm = _valid_archived_handoff(
            deployment_state='awaiting_gate',  # would require gate_dependency on active schema
        )
        errors = validate_frontmatter(fm, _HANDOFF_ARCHIVED_SCHEMA)
        # No cross-field error expected for awaiting_gate without gate_dependency
        assert not any(e['field'] == 'gate_dependency' for e in errors)

    def test_archived_required_fields_enforced(self):
        """Required fields are still enforced on archived handoffs."""
        fm = _valid_archived_handoff()
        del fm['title']
        errors = validate_frontmatter(fm, _HANDOFF_ARCHIVED_SCHEMA)
        assert any(e['field'] == 'title' for e in errors)

    def test_archived_enum_status_enforced(self):
        """Status enum is still enforced on archived handoffs."""
        fm = _valid_archived_handoff(status='bad_status')
        errors = validate_frontmatter(fm, _HANDOFF_ARCHIVED_SCHEMA)
        assert any(e['field'] == 'status' for e in errors)

    def test_archived_allows_superseded_status(self):
        """handoff-archived allows 'superseded' status (legacy tolerance)."""
        fm = _valid_archived_handoff(status='superseded')
        errors = validate_frontmatter(fm, _HANDOFF_ARCHIVED_SCHEMA)
        assert not any(e['field'] == 'status' for e in errors)


# ---------------------------------------------------------------------------
# match_schema archive-path precedence + tree-walk collection glob (F1-F3,
# code-reviewer 2026-07-27 on commit 93c52d84).
# ---------------------------------------------------------------------------

class TestArchivePathPrecedence:
    """match_schema(): a file under archive/handoffs/ resolves to
    handoff-archived (not the live handoff schema), scoped to a kind that
    is itself a handoff-family kind — see match_schema's kind-gate
    negative-spec."""

    def test_archived_path_with_handoff_kind_resolves_to_handoff_archived(self):
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        fm = {'kind': 'session-handoff'}
        resolved = match_schema('archive/handoffs/2026-07-05-foo.md', fm, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'handoff-archived'

    def test_live_path_with_handoff_kind_resolves_to_live_handoff_schema(self):
        """Same kind, live (non-archive) path — resolves to the CURRENT
        vocabulary schema, not handoff-archived. Pins the archive-path
        check as path-scoped, not a blanket kind-first inversion."""
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        fm = {'kind': 'session-handoff'}
        resolved = match_schema('state/handoffs/2026-07-05-foo.md', fm, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'handoff'

    def test_archived_path_with_non_handoff_kind_does_not_force_handoff_archived(self):
        """Finding 2: a misfiled non-handoff record under archive/handoffs/
        must NOT be force-validated against handoff-archived — its own
        declared kind wins. Uses a synthetic schemas dict (rather than the
        live vendored registry, which today registers `kind:` only for
        handoff) so the gate is exercised against a kind that resolves via
        _byKind to a genuinely different schema, isolating this from
        whatever kinds the real corpus happens to register."""
        schemas = {
            '_byGlob': [],
            '_byKind': {'session-handoff': 'handoff', 'other-record': 'other-schema'},
            'handoff': {'x-schema-name': 'handoff'},
            'handoff-archived': {'x-schema-name': 'handoff-archived'},
            'other-schema': {'x-schema-name': 'other-schema'},
        }
        fm = {'kind': 'other-record'}
        resolved = match_schema('archive/handoffs/2026-07-05-foo.md', fm, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'other-schema'

    def test_archived_path_with_no_kind_still_forces_handoff_archived(self):
        """No declared kind at all — path-only fallback still routes to
        handoff-archived (kind-gate only excludes a kind that resolves
        elsewhere, it does not require a kind to be present)."""
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        resolved = match_schema('archive/handoffs/2026-07-05-foo.md', None, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'handoff-archived'

    def test_retired_status_passes_archived_fails_live(self):
        """Finding 3(c): retired vocabulary tolerated by handoff-archived
        must still fail the live handoff schema, pinning the intended
        precedence behavior end-to-end through match_schema.

        Uses status='superseded', NOT the reviewer's originally-suggested
        'status: consumed' — DR-084 P4 (landed 2026-07-22) retired
        'consumed' from BOTH the handoff and handoff-archived enums, so it
        no longer demonstrates the archived/live divergence (it now fails
        both). 'superseded' is the vocabulary still uniquely tolerated by
        handoff-archived (see handoff-archived.schema.json's status
        description: legacy/archived read-tolerance) while rejected by the
        live handoff schema — this is the actual retired-vocabulary case
        that exercises the divergence today.
        """
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        fm = _valid_archived_handoff(kind='session-handoff', status='superseded')

        archived_resolved = match_schema('archive/handoffs/2026-07-05-foo.md', fm, schemas)
        assert archived_resolved['schemaName'] == 'handoff-archived'
        archived_result = validate_frontmatter_obj(fm, archived_resolved['schema'])
        assert not any(e['field'] == 'status' for e in (archived_result.get('errors') or []))

        live_resolved = match_schema('state/handoffs/2026-07-05-foo.md', fm, schemas)
        assert live_resolved['schemaName'] == 'handoff'
        live_result = validate_frontmatter_obj(fm, live_resolved['schema'])
        assert any(e['field'] == 'status' for e in (live_result.get('errors') or []))


class TestPlansIndexRoutingExclusion:
    """match_schema(): docs/plans/INDEX.md and docs/plans/README.md (the
    plan-collector's own directory-index exclusion set,
    `_PLAN_DIR_INDEX_FILENAMES`) resolve to None — not the `plan` schema's
    glob-fallback — when they carry no declared `kind:`. See match_schema's
    second-amendment docstring."""

    def test_index_md_no_frontmatter_resolves_to_none(self):
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        resolved = match_schema('docs/plans/INDEX.md', None, schemas)
        assert resolved is None

    def test_readme_md_no_frontmatter_resolves_to_none(self):
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        resolved = match_schema('docs/plans/README.md', None, schemas)
        assert resolved is None

    def test_real_plan_still_resolves_to_plan_schema(self):
        """Regression guard: an ordinary plan file is unaffected."""
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        resolved = match_schema('docs/plans/2026-01-01-a-real-plan.md', None, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'plan'

    def test_index_md_with_declared_kind_still_routes_via_kind(self):
        """Kind-gate: a docs/plans/INDEX.md that DOES declare a `kind:`
        resolving via `_byKind` is not routing-excluded — it still routes
        to that kind's schema, mirroring the archive-path kind-gate."""
        schemas = {
            '_byGlob': [],
            '_byKind': {'session-handoff': 'handoff'},
            'handoff': {'x-schema-name': 'handoff'},
        }
        fm = {'kind': 'session-handoff'}
        resolved = match_schema('docs/plans/INDEX.md', fm, schemas)
        assert resolved is not None
        assert resolved['schemaName'] == 'handoff'


class TestTreeWalkArchiveGlobCollection:
    """_run_tree_walk's collection-glob override (_GLOB_OVERRIDES) reaches
    nested archive/handoffs/<month>/*.md files that the vendored
    single-star applies_to glob cannot — exercised directly at the
    _lint_collect_files_for_glob level (the exact helper _run_tree_walk
    calls per schema/glob pair), avoiding a full git-repo + schema-registry
    fixture for what is fundamentally a glob-collection-depth assertion."""

    def test_nested_month_dir_reached_by_override_glob(self, tmp_path):
        nested = tmp_path / 'archive' / 'handoffs' / '2026-07'
        nested.mkdir(parents=True)
        (nested / 'foo.md').write_text('---\ntitle: x\n---\n', encoding='utf-8')

        results = _lint_collect_files_for_glob(str(tmp_path), _GLOB_OVERRIDES['handoff-archived'])
        repo_rels = {repo_rel for _, repo_rel in results}
        assert 'archive/handoffs/2026-07/foo.md' in repo_rels

    def test_vendored_glob_now_matches_override(self, tmp_path):
        """Schema 2.1.0 widened the vendored (coordinator-claude-owned SSOT) applies_to
        glob for handoff-archived from single-star to
        'archive/handoffs/**/*.md', closing the nested-month-dir gap
        _GLOB_OVERRIDES['handoff-archived'] was originally added to
        compensate for. Coordinator-claude declined to retire the override (it is also a
        query-records.js parity port, mirrored in
        coordinator_core/ops/records_query.py's _TYPE_TO_GLOB), so it is
        retained deliberately as belt-and-braces rather than an active
        compensation. This test pins that the SSOT and the override now
        AGREE — it fails loud if coordinator-claude ever re-narrows applies_to back to
        single-star, which would silently restore the nested-month-dir gap
        the override alone would then be silently compensating for again."""
        vendored = json.loads(_HANDOFF_ARCHIVED_SCHEMA.read_text(encoding='utf-8'))
        assert vendored['applies_to'] == _GLOB_OVERRIDES['handoff-archived']

    def test_override_scoped_to_handoff_archived_only(self):
        """Finding 1: the override dict must be consulted by literal key,
        never by iterating every loaded schema name — a future schema
        literally named 'cross-repo-memo' must not silently inherit the
        memo-inbox glob here. This asserts the dict itself still carries
        exactly the two documented keys (the scoping discipline lives in
        _run_tree_walk's `if name == 'handoff-archived'` guard, which has
        no independent unit surface — this pins the data half of that
        contract so a future key addition is a deliberate, reviewed edit)."""
        assert set(_GLOB_OVERRIDES.keys()) == {'handoff-archived', 'cross-repo-memo'}


# ---------------------------------------------------------------------------
# Schema version fail-loud
# ---------------------------------------------------------------------------

class TestSchemaVersionGate:
    def test_no_schema_version_in_record_is_ok(self):
        """Records without schema_version field are not gated (opt-in)."""
        fm = _valid_handoff()
        assert 'schema_version' not in fm
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_compatible_schema_version_ok(self):
        """Record schema_version major == schema major → ok."""
        fm = _valid_handoff(schema_version='1.0.0')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_compatible_minor_bump_ok(self):
        """Record schema_version minor > schema minor is OK (minor is back-compat)."""
        fm = _valid_handoff(schema_version='1.9.0')
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_newer_major_raises_schema_version_error(self):
        """Record schema_version major must be one PAST the live schema major, so the
        gate actually fires — derived from the live schema's `x-schema-version` here
        rather than a hardcoded literal.

        History: a literal pin went stale TWICE in one calendar day. First the
        placeholder-id-minting-guard narrow (`handoff.schema.json` 4.0.0 -> 5.0.0,
        narrowing the hnd- id patterns to exclude the scaffolder's placeholder slug)
        made the then-pin 5.0.0 equal the live major. That was "fixed" by bumping the
        literal to 6.0.0 — which a sibling repo's own 5.0.0 -> 6.0.0 bump then
        re-equalized the same day, on re-vendor. Each time the pin goes equal, this
        test stops exercising SchemaVersionError while still passing green — a
        silently-disarmed assertion, exactly the defect class this whole session has
        been chasing, sitting inside the test suite itself.

        Do NOT reintroduce a hardcoded literal here. Read `x-schema-version` off the
        live schema and use major + 1, so a future bump cannot disarm this assertion
        again."""
        live_major = int(
            json.loads(_HANDOFF_SCHEMA.read_text(encoding='utf-8'))['x-schema-version'].split('.')[0]
        )
        fm = _valid_handoff(schema_version=f'{live_major + 1}.0.0')
        with pytest.raises(SchemaVersionError, match='major'):
            validate_frontmatter(fm, _HANDOFF_SCHEMA)

    def test_schema_without_version_and_record_with_version_raises(self, tmp_path):
        """If schema lacks x-schema-version but record declares schema_version → fail loud."""
        schema_no_version = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'x-schema-name': 'no-version-schema',
            'type': 'object',
            'required': ['title'],
            'properties': {'title': {'type': 'string'}},
        }
        schema_path = tmp_path / 'no-version.schema.json'
        schema_path.write_text(json.dumps(schema_no_version))
        fm = {'title': 'test', 'schema_version': '1.0.0'}
        with pytest.raises(SchemaVersionError, match='x-schema-version'):
            validate_frontmatter(fm, schema_path)


# ---------------------------------------------------------------------------
# Drift check
# ---------------------------------------------------------------------------

class TestDriftCheck:
    def test_handoff_schema_matches_doe_head_after_dr084_revendor(self):
        """Vendored handoff.schema.json is byte-parity with coordinator-claude HEAD again, and both
        sides carry last_gate_recheck + the if/then construct as upstream-owned coordinator-claude
        constructs.

        The `pending_fix` mark this test carried from 2026-08-06 to 2026-08-13 is
        GONE, and the two halves it was gated on both cleared upstream in a single
        coordinator-claude commit (`3e6da5ce4e40`), re-vendored here at `8.0.0`:

        1. `properties.additional_predecessors.description` — claude-klabauter's local
           "ENGINE-DERIVED, not PM-directed" correction (roadmap stub sedge-02) is
           adopted VERBATIM upstream, so it is now an upstream-owned construct and a
           direct-copy re-vendor carries it rather than reverting it. The reason it
           had to move upstream and could not stay a local correction: a
           description-level fork is invisible to the obvious re-vendor move, which
           is exactly how the clobber at `1825e7771` came to exist.

        2. `x-schema-version` `7.1.0 -> 8.0.0`, `x-bump-class` `major` — coordinator-claude's
           `711ea128f` had removed `hand-authored` from
           `x-producer-typed-command.mapping` (a validation-SHAPE change) under an
           UNMOVED `7.1.0` that claude-klabauter had already vendored, deadlocking both trees:
           `test_vendored_schema_shape_bump_parity` here refuses a shape move at an
           unmoved version, and coordinator-claude's `test_vendored_schema_matches_doe_source`
           asserts version equality hard, so neither side could clear it alone. The
           version move was theirs and they made it. Byte-parity, not reachability,
           is what both gates read — deliberately: whether a consumer COULD have held
           the removed member is not a judgment a shape gate makes on a version
           string's behalf.

        Negative-spec for a future re-vendor: do NOT re-add `pending_fix` here to
        park a fresh divergence. This gate is live again; a new red means the
        vendored copy actually drifted from coordinator-claude HEAD and wants a re-vendor or a memo,
        not a mark.

        Formerly `test_handoff_schema_diverges_from_doe_head_intentionally`: C1's
        schema-hardening sub-step (docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md
        § C1) had added last_gate_recheck (property declaration) and a JSON-Schema-level
        if/then belt-and-suspenders mirror of the ready_to_fire->gate_dependency-forbidden
        Python cross-field rule as claude-klabauter-local hardening on top of the vendored file,
        intentionally diverging from coordinator-claude HEAD.

        The DR-084 P0 dual-vocabulary re-vendor (03b8a127, status enum += open/claimed,
        deployment_state enum += continued/closed, new nullable claimed_at/claimed_by/
        continued_into/closed_reason fields) replaced the vendored file wholesale from
        coordinator-claude HEAD 6082a287, which did not carry the C1 local hardening forward — a
        straight re-vendor, not a merge, so both constructs were dropped. Claude-klabauter
        memo'd coordinator-claude asking for them to be re-added upstream; coordinator-claude landed the re-add at
        bfbaac70 ("schema: re-add last_gate_recheck property + ready_to_fire if/then
        dropped by DR-084 P0 widen (claude-klabauter memo)"). This re-vendor pulls in bfbaac70,
        so both constructs are back — now as upstream-owned coordinator-claude constructs, not
        claude-klabauter-local drift.

        This asserts the current invariant: check_schema_drift must NOT raise for
        handoff.schema.json (matching the sibling handoff-archived assertion below),
        and both sides declare last_gate_recheck and an allOf-nested if/then carrying
        the ready_to_fire construct (DR-084 P4 narrow, d652253c, restructured the
        former top-level if/then into one clause of a 4-entry allOf array) — catching
        a future re-vendor that silently drops either.
        """
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(_HANDOFF_SCHEMA, _DOE_REPO)  # should not raise

        vendored = json.loads(_HANDOFF_SCHEMA.read_text(encoding='utf-8'))
        doe_result = subprocess.run(
            ['git', '-C', str(_DOE_REPO), 'show', 'HEAD:coordinator/schemas/handoff.schema.json'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=30,
        )
        assert doe_result.returncode == 0, (
            f'Cannot read coordinator-claude HEAD handoff.schema.json: {doe_result.stderr.strip()}'
        )
        doe_schema = json.loads(doe_result.stdout)

        # The former C1 local hardening is now upstream-owned, present on BOTH sides.
        assert 'last_gate_recheck' in vendored['properties'], (
            'vendored handoff.schema.json no longer declares last_gate_recheck — '
            'this test\'s premise (coordinator-claude re-added it at bfbaac70) may be stale, revisit'
        )
        assert 'last_gate_recheck' in doe_schema.get('properties', {}), (
            'coordinator-claude HEAD no longer declares last_gate_recheck — re-verify against current '
            'coordinator-claude HEAD, this test\'s premise may be stale'
        )

        def _has_if_then_construct(schema: dict) -> bool:
            if 'if' in schema and 'then' in schema:
                return True
            return any(
                isinstance(clause, dict) and 'if' in clause and 'then' in clause
                for clause in schema.get('allOf', []) or []
            )

        assert _has_if_then_construct(vendored), (
            'vendored handoff.schema.json no longer declares an if/then construct '
            '(top-level or allOf-nested) — this test\'s premise (coordinator-claude re-added it at '
            'bfbaac70, restructured under allOf at d652253c) may be stale, revisit'
        )
        assert _has_if_then_construct(doe_schema), (
            'coordinator-claude HEAD no longer declares an if/then construct (top-level or allOf-nested) — '
            're-verify against current coordinator-claude HEAD, this test\'s premise may be stale'
        )

    def test_handoff_archived_schema_matches_doe_head(self):
        """Vendored handoff-archived.schema.json must match coordinator-claude HEAD."""
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(_HANDOFF_ARCHIVED_SCHEMA, _DOE_REPO)  # should not raise

    def test_drift_detected_on_modified_schema(self, tmp_path):
        """SchemaDriftError raised when vendored schema content differs from coordinator-claude HEAD."""
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        # Create a modified copy of the schema
        original = _HANDOFF_SCHEMA.read_text()
        modified = original + '\n// extra line that makes it diverge\n'
        modified_path = tmp_path / 'handoff.schema.json'
        modified_path.write_text(modified)
        with pytest.raises(SchemaDriftError, match='diverges'):
            check_schema_drift(modified_path, _DOE_REPO)

    def test_drift_check_invalid_doe_repo_raises(self, tmp_path):
        """SchemaDriftError raised when doe_repo_path is not a valid git repo."""
        not_a_repo = tmp_path / 'not_a_repo'
        not_a_repo.mkdir()
        with pytest.raises(SchemaDriftError):
            check_schema_drift(_HANDOFF_SCHEMA, not_a_repo)

    def test_plan_tasks_schema_matches_doe_head(self):
        """Vendored plan-tasks.schema.json is byte-parity with coordinator-claude HEAD — no
        divergence, ratified or otherwise.

        SUPERSEDES `test_plan_tasks_schema_matches_doe_head_net_of_ratified_split`,
        which stripped `grouping_approvals` (+ its `$defs` block) before comparing
        because claude-klabauter declared that contract here while coordinator-claude declared it on
        plan.schema.json. That divergence is GONE as of the 2026-08-06 re-vendor:
        claude-klabauter now vendors plan.schema.json too (see the sibling test below), and
        plan-tasks.schema.json is a clean copy of coordinator-claude HEAD.

        The strip-then-compare shape encoded coordinator-claude's **2026-07-29** ruling ("I am not
        asking you to move yours ... assert parity on the `grouping_approvals` shape
        sub-object, not on the declaring filename" —
        cross-repo/archive/2026-07-29-coordinator-claude-em-grouping-discriminator-correction.md).
        coordinator-claude **superseded** that on 2026-07-31
        (cross-repo/archive/2026-07-31-coordinator-claude-em-grouping-approval-vendor-home.md:
        "Add plan.schema.json to your vendored set ... Drop the block from your
        vendored plan-tasks.schema.json and re-vendor that file clean from our
        HEAD"), and re-asked on 2026-08-06 when their parity gate went
        `direction='both'`. The old test's own docstring carried a "this test's
        premise may be stale, revisit" escape hatch on exactly this assertion; this
        is that revisit.

        Note the block was never load-bearing here even while declared:
        plan-tasks.schema.json is a PER-ROW schema (`x-schema-name: plan-tasks`)
        with no `additionalProperties: false`, so a plan-DOCUMENT-level property
        declared in it was never exercised against a plan document. No runtime
        reader resolved it out of this file either — `is_governed_plan` takes a
        frontmatter dict, never a schema file.
        """
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(_PLAN_TASKS_SCHEMA, _DOE_REPO)  # should not raise

        vendored = json.loads(_PLAN_TASKS_SCHEMA.read_text(encoding='utf-8'))
        assert 'grouping_approvals' not in vendored.get('properties', {}), (
            'grouping_approvals is back on plan-tasks.schema.json -- the 2026-07-31 '
            'relocation to plan.schema.json has been undone, most likely by a '
            'hand-edit of a vendored artifact (the exact failure mode coordinator-claude raised). '
            'Re-vendor via bin/claude-klabauter-revendor-schema.py plan-tasks.'
        )
        assert 'grouping_approval_block' not in vendored.get('$defs', {}), (
            '$defs.grouping_approval_block is back on plan-tasks.schema.json -- see '
            'the grouping_approvals assertion above; the contract lives on '
            'plan.schema.json.'
        )
        assert 'depends_on' in vendored.get('properties', {}), (
            'plan-tasks.schema.json no longer declares depends_on -- the 1.5.0 '
            'task-spine-dependency-declaration field was dropped by a re-vendor '
            'against an older coordinator-claude ref. docs/wiki/writing-plans.md instructs authors '
            'to write it, so dropping it re-opens the schema/wiki contradiction '
            'coordinator-claude 1.5.0 closed.'
        )

    def test_plan_schema_matches_doe_head_and_homes_grouping_approvals(self):
        """Vendored plan.schema.json is byte-parity with coordinator-claude HEAD, and is the home
        of the grouping-approval contract.

        REPLACES `test_plan_tasks_schema_grouping_approvals_shape_parity`, which
        compared claude-klabauter's plan-tasks-declared block against coordinator-claude's
        plan.schema.json-declared one with prose stripped. That cross-file,
        shape-only comparison existed precisely BECAUSE the two sides declared the
        contract in different files; now that claude-klabauter vendors plan.schema.json
        byte-for-byte, whole-file drift subsumes shape parity entirely and the
        stripped comparison would be a strictly weaker duplicate of this check.

        What still needs asserting is that the contract has a home at all: the
        2026-07-31 memo warned that "dropping the block without taking the
        relocation leaves you with no home for it at all". These two assertions are
        that guard.
        """
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(_PLAN_SCHEMA, _DOE_REPO)  # should not raise

        vendored = json.loads(_PLAN_SCHEMA.read_text(encoding='utf-8'))
        assert 'grouping_approvals' in vendored.get('properties', {}), (
            'plan.schema.json no longer declares grouping_approvals -- the '
            'grouping-approval contract now has NO vendored home (it was relocated '
            'off plan-tasks.schema.json on 2026-07-31 precisely so this file could '
            'hold it). check_plan_tasks_grouping_approval enforces a contract '
            'nothing declares.'
        )
        assert 'grouping_approval_block' in vendored.get('$defs', {}), (
            'plan.schema.json no longer declares $defs.grouping_approval_block -- '
            'see the grouping_approvals assertion above.'
        )

    def test_plan_tasks_drift_detected_on_modified_schema(self, tmp_path):
        """SchemaDriftError raised when vendored plan-tasks schema content differs from coordinator-claude HEAD."""
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        original = _PLAN_TASKS_SCHEMA.read_text()
        modified = original + '\n// extra line that makes it diverge\n'
        modified_path = tmp_path / 'plan-tasks.schema.json'
        modified_path.write_text(modified)
        with pytest.raises(SchemaDriftError, match='diverges'):
            check_schema_drift(modified_path, _DOE_REPO)


# ---------------------------------------------------------------------------
# Pinned-SHA tamper-check: the 8 queue schemas, pinned per-schema
# ---------------------------------------------------------------------------

# Pins are per-schema, not a single shared scalar, because coordinator-claude advances
# individual queue schemas independently — a shared pin forces an
# all-or-nothing re-vendor of all 8 schemas even when only one has moved.
#
# _C1_LANDING_SHA is the original C1 landing SHA, verified an ancestor of coordinator-claude
# origin/main at vendor-time. It still pins the seven schemas that have not
# diverged since C1.
#
# improvement-queue's pin, b1e1643afe8ca0ec1ac379f8a8dbaa170323ada3, is NOT an
# ancestor of coordinator-claude origin/main. It currently lives only on coordinator-claude's
# work/machine-a/2026-07-21 (pushed to origin/work/machine-a/2026-07-21) — it is
# fetch-reachable but not yet merged. This matches the same-day precedent set
# by the handoff.schema.json re-vendor at 6082a287, which was also cut from
# that unmerged branch. Residual risk is explicit: if coordinator-claude rebases or drops
# that branch, this pin becomes unresolvable and the tamper-check fails loud
# — that is the correct, intended failure mode, not a silent pass.
#
# All values below are hardcoded literals, not discovered from a
# memo/commitment parser.
_C1_LANDING_SHA = "758de78b11d70b4914cb5592f96648172037332c"

# review-findings' pin, 3203e9c1b9e7b8549ab419b731151fe30ec2e3ab, re-vendors
# the v2.0.0 frontmatter-required shape (drops match_mode:no-frontmatter,
# required:["status"], mirrors run-report.schema.json) since coordinator-
# doc-new's self-scaffold fallback (claude-klabauter commit b0a336c4, then
# frontmatter-unification follow-up) now emits the same frontmatter shape
# as provision_report.py's provisioned path. Same not-yet-merged-branch
# precedent as improvement-queue's pin above: this SHA currently lives only
# on coordinator-claude's work/machine-a/2026-07-21 (fetch-reachable, not yet merged to
# origin/main).
#
# cross-repo-commitment's pin, 472774939940d4372886359778bc0a174c102c26, re-vendors
# v1.1.0 — an optional top-level `declaration` object carrying the DR-097
# sibling-notification-duty fields. Same not-yet-merged-branch precedent as the
# two pins above: this SHA currently lives only on coordinator-claude's
# work/machine-b/2026-07-21to26 (fetch-reachable, not yet merged to origin/main).
# Note the class: an optional top-level *object*, so it is NOT the
# top-level-array-additive class claude-klabauter's structural-tolerance ratification
# covers — this pin moved by re-vendor, not by a tolerance carve-out.
_QUEUE_SCHEMA_PINS = {
    'bug-backlog': _C1_LANDING_SHA,
    'cross-repo-commitment': "472774939940d4372886359778bc0a174c102c26",
    'debt-backlog': _C1_LANDING_SHA,
    # Pin moved 2026-07-29 to 9f6ee8540e7b09da9ce6b81509402a4f118aefd8 (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py improvement-queue.
    #   coordinator-claude 1239761c1 added the 'verification' member; b142e8dc re-vendored
    #   plan-tasks and admitted it to the harvest routing set but left the
    #   improvement-queue write target rejecting it
    # Pin moved 2026-07-29 to a7723f2c26d855000db36e0c77cbf75ce7b8b01b (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py improvement-queue.
    #   config-edit was authorable in coordinator-claude's enum but absent from claude-klabauter's
    #   vendored copy, so plan.tasks.mutate refused every row on any plan
    #   using it, blocking delivery recording for a fully-executed plan. PM-
    #   authorized 2026-07-29. plan-tasks re-vendored in the same pass to
    #   replace a hand-edit with byte-identical content.
    # Pin moved 2026-08-06 to 8a1f74c52a1c90faa744269bbde300bf1edd36e4 (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py improvement-queue.
    #   coordinator-claude 1.0.0 -> 1.1.0 change_kind enum widen (verification, config-edit);
    #   claude-klabauter's copy already carried both members, so this takes the version
    #   string and bump metadata only. Verified version-string-only by diff
    #   before running. Coordinator-claude held merge-to-main on this confirmation (handoff
    #   2026-08-03-vendored-schema-re-vendor-round item 3).
    # Pin moved 2026-08-06 to 942745f317a5194e2b349166046c5dec1392f37e (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py improvement-queue.
    #   coordinator-claude 3cfaef61e: case_against on the task row (required on
    #   backlogged/wont_do) and optional on the improvement-queue entry —
    #   plan-tasks 1.5.0->1.6.0, improvement-queue 1.1.0->1.2.0, both nested-
    #   field-additive. Asked by coordinator-claude-em, cross-
    #   repo/inbox/2026-08-06-coordinator-claude-em-deferrals-both-sides-landed-
    #   revendor-and-a-resolve-ergonomics-bug.md
    # Pin moved 2026-08-06 to 9b5a08fd2e0cebe22a8133a630304b1c253deabe (coordinator-claude
    # 9b5a08fd2) by bin/claude-klabauter-revendor-schema.py improvement-queue.
    #   Equal-version (1.2.0) content drift on case_against.description:
    #   claude-klabauter carried the pre-697b7d451 'not yet populated by the harvest
    #   CLI' prose, coordinator-claude 9b5a08fd2 carries the post-carry-through reading
    #   (omit-vs-empty-string rationale). Semantics unchanged — optional
    #   either way. Re-vendor per coordinator-claude-em memo 2026-08-06-coordinator-claude-em-
    #   improvement-queue-revendor-equal-version-drift; neither side's gate
    #   catches equal-version content drift, which is the residual worth
    #   noting.
    'improvement-queue': "9b5a08fd2e0cebe22a8133a630304b1c253deabe",
    'lesson-entry': _C1_LANDING_SHA,
    'lessons-outbox': _C1_LANDING_SHA,
    # Pin moved 2026-08-13 to a88486a268af18ebc2b751339ec6f56d1ce1cb88 (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py review-findings.
    #   re-vendor: coordinator-claude bumped x-schema-version and changed shape
    #   (2.0.0->2.1.0, 1.0.0->1.1.0), adding optional reviewed_range array;
    #   confirmed divergence_kind=shape via schema_drift_watch
    'review-findings': "a88486a268af18ebc2b751339ec6f56d1ce1cb88",
    # Moved off _C1_LANDING_SHA 2026-07-27: coordinator-claude landed the optional
    # `reviewed_paths` property at x-schema-version 1.1.0 (their 89c24b12d), in
    # response to this repo's canonical-first ask. Re-vendored from that commit;
    # this pin is a byte-identity pin on coordinator-claude's file, distinct from coordinator-claude's own
    # canonical-shape content hash in schema-version-pins.json.
    # Pin moved 2026-08-10 to 840491558109540f7416e6f09c78148f336873ec (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py review-trail.
    #   coordinator-claude vendored claude-klabauter's 1.2.0 scope_kind enum at 6baac04a3; the ahead-
    #   pin's own remedy path says remove it and restore an ordinary byte-pin
    #   at the new shared SHA
    # Pin moved 2026-08-13 to 6466d871410baa349c1836286d5a8a1f1b5b5bcb (coordinator-claude
    # HEAD) by bin/claude-klabauter-revendor-schema.py review-trail.
    #   coordinator-claude 1.3.0 adds optional execution_basis; parity gate red per memo
    #   2026-08-13-coordinator-claude-em-bump-class-deliberately-absent.md
    'review-trail': "6466d871410baa349c1836286d5a8a1f1b5b5bcb",
    # Vendored 2026-08-06 (initial vendoring, by hand — see
    # bin/claude-klabauter-revendor-schema.py's own docstring for why the FIRST
    # vendoring of a not-yet-tracked name is done by hand, not by the
    # script): priority-ledger.schema.json is the SOLE path-traversal
    # defense on the priority.set write path (target_id -> filename), and
    # the docstrings' own "may not exist yet" premise for the prior
    # live-coordinator-claude-tree read expired 2026-07-27 when this schema landed in coordinator-claude.
    # Pinned to 577a710c7 (x-schema-version 1.1.0), the ref confirmed clean
    # and present in coordinator-claude at vendor-time.
    # docs/plans/2026-08-06-vendor-priority-ledger-and-priority-inte.md § C1
    'priority-ledger': "577a710c7c07cbeb0b061ebcc131dc09d2975654",
    # Vendored 2026-08-06, same wave as priority-ledger above — see that
    # entry's comment. priority-intent.schema.json is the record shape
    # example-cockpit-repo drops into priority-intent-inbox/ for priority.drain
    # to consume; its target_id pattern is priority_drain.py's own
    # non-skippable trust boundary. Pinned to 577a710c7 (x-schema-version
    # 1.1.0), same ref as priority-ledger.
    # docs/plans/2026-08-06-vendor-priority-ledger-and-priority-inte.md § C1
    'priority-intent': "577a710c7c07cbeb0b061ebcc131dc09d2975654",
}

# Ahead-pin registry: entries here declare "claude-klabauter's vendored copy is
# intentionally ahead of coordinator-claude's, awaiting coordinator-claude's own upward vendor" — the
# check_schema_ahead_of_doe counterpart to _QUEUE_SCHEMA_PINS's byte-identity
# tamper-pin (check_schema_drift). A name present here is checked via
# check_schema_ahead_of_doe instead of check_schema_drift, even though its
# entry also still exists in _QUEUE_SCHEMA_PINS above (kept as the historical
# byte-pin value to restore once coordinator-claude catches up — see doe_ref below).
#
# Each entry's VALUE is a dict of check_schema_ahead_of_doe's keyword args:
#   doe_ref (str, required)     — the coordinator-claude SHA this ahead-pin was derived against.
#   reason (str, required)      — why claude-klabauter is allowed to lead coordinator-claude here.
#   provenance (str, required)  — commit/memo/plan that authorized it.
#   exempt_paths (frozenset, optional) — per-schema leaf-retention exemptions
#     (consumer-specific paths — see _AHEAD_RETENTION_EXEMPT_PATHS's docstring
#     comment in schema_validate.py for why these live per-entry, not module-wide).
#   local_shape_hash (str, optional) — the ahead-state's own local tamper-pin
#     (schema_validate._local_shape_hash(current vendored text)); recompute
#     and update it whenever the vendored copy is legitimately revised while
#     ahead.
#
# WIRED: TestAheadPinRegistryRouting below iterates every key in this dict and
# routes it through check_schema_ahead_of_doe — trivially green while this is
# empty, but it fails the moment an entry is added without the corresponding
# schema file existing / without the check passing, which is exactly the "the
# registry does nothing" rot P1-3 identified. Review: eng-director P1-3.
#
# review-trail: claude-klabauter bumped to 1.2.0 (closing scope_kind from an
# unconstrained string to the enum ["diff","plan","integration"]) to fix a
# live crash — an out-of-set scope_kind value was taking down the whole
# coverage gate with an AssertionError. Coordinator-claude is still at 1.1.0 and has not
# moved "coordinator/schemas/review-trail.schema.json" since the pinned SHA
# below (git diff <doe_ref> -- that path in coordinator-claude is empty). Claude-klabauter's
# 1.2.0 is a structural superset of coordinator-claude's 1.1.0 except for scope_kind's
# description, deliberately rewritten because the 1.1.0 prose named "chunk"
# as a valid example, which the 1.2.0 enum excludes.
#
# Once coordinator-claude vendors 1.2.0 (or later): delete this entry, and move
# _QUEUE_SCHEMA_PINS['review-trail'] to the new shared SHA so
# test_review_trail_matches_pinned_sha goes back to a plain check_schema_drift
# byte-pin call below — that is the designed exit path, not a place to leave
# this parked indefinitely.
# Schemas where claude-klabauter deliberately leads coordinator-claude, awaiting their upward vendor.
# EMPTY IS THE HEALTHY STATE — an entry here is a temporary divergence with a
# named reason, not a resting place, and `check_schema_ahead_of_doe` fails the
# moment coordinator-claude moves so the entry cannot quietly outlive its justification.
#
# review-trail occupied this for a few hours on 2026-08-10 and is the worked
# example: claude-klabauter closed `scope_kind` to an enum ahead of coordinator-claude to fix a live
# coverage-gate crash (55cbf4ede), the ahead-pin held the gate honest rather
# than muting it, coordinator-claude vendored the same change themselves at their 6baac04a3,
# the stale-ahead branch caught that unprompted, and the pair converged back to
# an ordinary byte-pin through bin/claude-klabauter-revendor-schema.py. That is the whole
# intended lifecycle: declare, gate, converge, remove.
_QUEUE_SCHEMA_AHEAD_PINS: dict = {}

_QUEUE_SCHEMA_NAMES = (
    'bug-backlog',
    'cross-repo-commitment',
    'debt-backlog',
    'improvement-queue',
    'lesson-entry',
    'lessons-outbox',
    'review-findings',
    'review-trail',
)


class TestInferDriftDirection:
    """Unit coverage for _infer_drift_direction — the structural + text-fallback
    best-effort AHEAD/BEHIND/BOTH read consumed by check_schema_drift_advisory and
    surfaced through coordinator_core.frontmatter.schema_drift_watch.

    Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
    """

    def test_local_only_field_is_we_are_ahead(self) -> None:
        local = json.dumps({"properties": {"a": {"type": "string"}, "b": {"type": "string"}}})
        doe = json.dumps({"properties": {"a": {"type": "string"}}})
        assert _infer_drift_direction(local, doe) == DIRECTION_WE_AHEAD

    def test_doe_only_field_is_we_are_behind(self) -> None:
        local = json.dumps({"properties": {"a": {"type": "string"}}})
        doe = json.dumps({"properties": {"a": {"type": "string"}, "b": {"type": "string"}}})
        assert _infer_drift_direction(local, doe) == DIRECTION_WE_BEHIND

    def test_additions_on_both_sides_is_both(self) -> None:
        local = json.dumps({"properties": {"a": {"type": "string"}, "local_only": {"type": "string"}}})
        doe = json.dumps({"properties": {"a": {"type": "string"}, "doe_only": {"type": "string"}}})
        assert _infer_drift_direction(local, doe) == DIRECTION_BOTH

    def test_shared_leaf_extended_on_doe_side_is_we_are_behind(self) -> None:
        local = json.dumps({"title": "short"})
        doe = json.dumps({"title": "short but longer now"})
        assert _infer_drift_direction(local, doe) == DIRECTION_WE_BEHIND

    def test_shared_leaf_extended_on_local_side_is_we_are_ahead(self) -> None:
        local = json.dumps({"title": "short but longer now"})
        doe = json.dumps({"title": "short"})
        assert _infer_drift_direction(local, doe) == DIRECTION_WE_AHEAD

    def test_unrelated_value_change_is_both(self) -> None:
        local = json.dumps({"title": "apples"})
        doe = json.dumps({"title": "oranges"})
        assert _infer_drift_direction(local, doe) == DIRECTION_BOTH

    def test_non_json_local_addition_falls_back_to_text_containment_ahead(self) -> None:
        # doe's text is a substring of local's -> local extended doe's content -> AHEAD.
        assert _infer_drift_direction("not json {{{", "not json") == DIRECTION_WE_AHEAD

    def test_non_json_doe_addition_falls_back_to_text_containment(self) -> None:
        assert _infer_drift_direction("not json", "not json {{{") == DIRECTION_WE_BEHIND

    def test_non_json_unrelated_text_is_both(self) -> None:
        assert _infer_drift_direction("not json alpha", "not json beta") == DIRECTION_BOTH


def _advisory_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-C', str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


class TestAdvisoryLocalDoeVersions:
    """local_version/doe_version on check_schema_drift_advisory — the x-schema-version
    read threaded through for the cross-repo ask (coordinator-claude wants both sides'
    version integers, not just a diverged/matched boolean).

    Every fixture here is a throwaway tmp_path git repo, never the real coordinator-claude clone —
    same discipline as coordinator_core/frontmatter/tests/test_schema_drift_watch.py.

    Spec backlink: cross-repo/inbox/2026-07-26-coordinator-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md
    """

    @pytest.fixture()
    def fake_doe(self, tmp_path: Path) -> Path:
        if not _which_git():
            pytest.skip("git not available")
        repo = tmp_path / "coordinator-claude-fake"
        schemas = repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps({"x-schema-version": "1.0.0", "title": "widget"}, indent=2) + "\n",
            encoding="utf-8",
        )
        _advisory_git(repo, "init", "-q")
        _advisory_git(repo, "config", "user.email", "test@example.invalid")
        _advisory_git(repo, "config", "user.name", "advisory version test")
        _advisory_git(repo, "add", "-A")
        _advisory_git(repo, "commit", "-q", "-m", "seed widget schema")
        return repo

    def test_match_populates_both_versions(self, fake_doe: Path, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps({"x-schema-version": "1.0.0", "title": "widget"}, indent=2) + "\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        assert result["local_version"] == "1.0.0"
        assert result["doe_version"] == "1.0.0"

    def test_drift_populates_differing_versions(self, fake_doe: Path, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps({"x-schema-version": "1.1.0", "title": "widget (local bump)"}, indent=2) + "\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["local_version"] == "1.1.0"
        assert result["doe_version"] == "1.0.0"

    def test_unreadable_doe_side_yields_both_none(self, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps({"x-schema-version": "1.0.0"}, indent=2) + "\n", encoding="utf-8"
        )
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = check_schema_drift_advisory(vendored, not_a_repo)
        assert result["determinate"] is False
        assert result["local_version"] is None
        assert result["doe_version"] is None

    def test_unreadable_local_side_still_reports_doe_version(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        # Named "widget.schema.json" (matches fake_doe's ref) but never written to
        # disk — the vendored-read must fail while the coordinator-claude-side read already
        # succeeded, exercising the "doe_version populated, local_version None"
        # branch specifically.
        missing_vendored = tmp_path / "widget.schema.json"
        assert not missing_vendored.exists()
        result = check_schema_drift_advisory(missing_vendored, fake_doe)
        assert result["determinate"] is False
        assert result["local_version"] is None
        assert result["doe_version"] == "1.0.0"

    def test_missing_key_or_unparseable_yields_none(self, fake_doe: Path, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text("not json at all {{{", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["local_version"] is None
        assert result["doe_version"] == "1.0.0"


class TestReadBumpClass:
    """_read_bump_class / _read_bump_note — the x-bump-class/x-bump-note extraction
    seam, same best-effort/None-on-failure contract as _read_schema_version. The
    JSON parse happens exactly once per string via the shared
    _parse_schema_dict/_read_schema_string_key seam (see schema_validate.py).

    Spec backlink: cross-repo/inbox/2026-07-27-coordinator-claude-em-bump-class-shipped-and-a-correction.md
    """

    def test_present_string_value(self) -> None:
        content = json.dumps({"x-bump-class": "nested-field-additive"})
        assert _read_bump_class(content) == "nested-field-additive"

    def test_absent_key_yields_none(self) -> None:
        content = json.dumps({"x-schema-version": "1.0.0"})
        assert _read_bump_class(content) is None

    def test_malformed_json_yields_none(self) -> None:
        assert _read_bump_class("not json at all {{{") is None

    def test_non_string_value_yields_none(self) -> None:
        content = json.dumps({"x-bump-class": 3})
        assert _read_bump_class(content) is None

    def test_non_object_top_level_yields_none(self) -> None:
        assert _read_bump_class(json.dumps(["not", "an", "object"])) is None

    def test_bump_note_present_string_value(self) -> None:
        content = json.dumps({"x-bump-note": "renamed a field, no shape change"})
        assert _read_bump_note(content) == "renamed a field, no shape change"

    def test_bump_note_absent_key_yields_none(self) -> None:
        assert _read_bump_note(json.dumps({"x-bump-class": "major"})) is None

    def test_bump_note_malformed_json_yields_none(self) -> None:
        assert _read_bump_note("not json at all {{{") is None

    def test_bump_note_non_string_value_yields_none(self) -> None:
        assert _read_bump_note(json.dumps({"x-bump-note": ["list", "not", "str"]})) is None


class TestAdvisoryBumpClassPassthrough:
    """local_bump_class/doe_bump_class/doe_bump_note on check_schema_drift_advisory —
    additive keys, same populated-whenever-readable contract as local_version/
    doe_version. Upstream adoption of x-bump-class is deliberately partial (DR-097
    memo), so absence is an ordinary None here, never an error.

    Spec backlink: cross-repo/inbox/2026-07-27-coordinator-claude-em-bump-class-shipped-and-a-correction.md
    """

    @pytest.fixture()
    def fake_doe(self, tmp_path: Path) -> Path:
        if not _which_git():
            pytest.skip("git not available")
        repo = tmp_path / "coordinator-claude-fake"
        schemas = repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps(
                {
                    "x-schema-version": "1.0.0",
                    "x-bump-class": "nested-field-additive",
                    "x-bump-note": "added an optional field",
                    "title": "widget",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _advisory_git(repo, "init", "-q")
        _advisory_git(repo, "config", "user.email", "test@example.invalid")
        _advisory_git(repo, "config", "user.name", "advisory bump class test")
        _advisory_git(repo, "add", "-A")
        _advisory_git(repo, "commit", "-q", "-m", "seed widget schema")
        return repo

    def test_drift_populates_bump_class_and_note_from_doe_side(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(
                {"x-schema-version": "1.0.0", "title": "widget (local, no bump-class yet)"},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["local_bump_class"] is None
        assert result["doe_bump_class"] == "nested-field-additive"
        assert result["doe_bump_note"] == "added an optional field"

    def test_match_populates_bump_class_both_sides(self, fake_doe: Path, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(
                {
                    "x-schema-version": "1.0.0",
                    "x-bump-class": "nested-field-additive",
                    "x-bump-note": "added an optional field",
                    "title": "widget",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        assert result["local_bump_class"] == "nested-field-additive"
        assert result["doe_bump_class"] == "nested-field-additive"

    def test_unreadable_doe_side_yields_bump_fields_none(self, tmp_path: Path) -> None:
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps({"x-schema-version": "1.0.0"}), encoding="utf-8")
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = check_schema_drift_advisory(vendored, not_a_repo)
        assert result["determinate"] is False
        assert result["local_bump_class"] is None
        assert result["doe_bump_class"] is None
        assert result["doe_bump_note"] is None

    def test_no_bump_class_reaches_same_status_as_before_the_feature(
        self, tmp_path: Path
    ) -> None:
        """Regression: a schema pair with no x-bump-class anywhere still reaches
        the same diverged/determinate verdict as pre-bump-class behavior — the new
        keys are additive-only, never a shape/verdict change."""
        if not _which_git():
            pytest.skip("git not available")
        repo = tmp_path / "coordinator-claude-fake-no-bump-class"
        schemas = repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps({"x-schema-version": "1.0.0", "title": "widget"}, indent=2) + "\n",
            encoding="utf-8",
        )
        _advisory_git(repo, "init", "-q")
        _advisory_git(repo, "config", "user.email", "test@example.invalid")
        _advisory_git(repo, "config", "user.name", "advisory regression test")
        _advisory_git(repo, "add", "-A")
        _advisory_git(repo, "commit", "-q", "-m", "seed widget schema, no bump class")

        # Named "widget.schema.json" so the advisory finds it at coordinator-claude HEAD.
        vendored_named = tmp_path / "widget.schema.json"
        vendored_named.write_text(
            json.dumps({"x-schema-version": "1.0.0", "title": "widget"}, indent=2) + "\n",
            encoding="utf-8",
        )

        result = check_schema_drift_advisory(vendored_named, repo)
        assert result["diverged"] is False
        assert result["determinate"] is True
        assert result["local_bump_class"] is None
        assert result["doe_bump_class"] is None
        assert result["doe_bump_note"] is None


def _which_git() -> bool:
    import shutil
    return shutil.which("git") is not None


_CANONICAL_WIDGET: dict = {
    "x-schema-version": "1.0.0",
    "x-bump-class": "nested-field-additive",
    "x-bump-note": "added an optional field",
    "$comment": "original comment",
    "title": "widget",
}


class TestCanonicalDriftAdvisory:
    """check_schema_drift_advisory's canonical-JSON comparison — C2 of
    docs/plans/2026-08-03-vendored-schema-drift-canonical-normalization.md, one
    test per AC in that chunk's body, plus a direct AC4 assertion against the
    gating sibling `check_schema_drift`.

    Every fixture here is a throwaway tmp_path git repo, never the real coordinator-claude
    clone — same discipline as TestAdvisoryLocalDoeVersions/
    TestAdvisoryBumpClassPassthrough above and test_schema_drift_watch.py's
    fake-coordinator-claude-repo helpers.

    Spec backlink: cross-repo/inbox/2026-08-03-coordinator-claude-em-drift-normalize-yes-but-comment-survives-canonicalization.md
    """

    @pytest.fixture()
    def fake_doe(self, tmp_path: Path) -> Path:
        if not _which_git():
            pytest.skip("git not available")
        repo = tmp_path / "coordinator-claude-fake"
        schemas = repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps(_CANONICAL_WIDGET, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _advisory_git(repo, "init", "-q")
        _advisory_git(repo, "config", "user.email", "test@example.invalid")
        _advisory_git(repo, "config", "user.name", "canonical drift advisory test")
        _advisory_git(repo, "add", "-A")
        _advisory_git(repo, "commit", "-q", "-m", "seed widget schema")
        return repo

    def test_ac1_formatting_only_delta_reports_not_diverged(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC1: reindented, key-reordered, trailing-newline-added copy of the
        SAME JSON value reports diverged=False, determinate=True, direction=None."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(_CANONICAL_WIDGET, indent=4, sort_keys=False) + "\n\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        assert result["determinate"] is True
        assert result["direction"] is None

    def test_ac2a_non_comment_value_edit_still_diverges(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC2a: an ordinary (non-$comment) value edit still reports
        diverged=True with a non-None direction — canonicalization only
        absorbs formatting, never a value change."""
        edited = dict(_CANONICAL_WIDGET)
        edited["title"] = "widget (locally edited)"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["direction"] is not None

    def test_ac2b_comment_prose_only_delta_no_longer_diverges(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC2b — D1 RULING (docs/plans/2026-08-03-vendored-schema-drift-
        canonical-normalization.md D1, ratified — CLOSED, not open): a
        `$comment` leaf differing ONLY in prose reports diverged=False. The
        vendored copy exists to mirror schema SEMANTICS, and `$comment` is by
        JSON-Schema definition a non-semantic annotation carrying no
        validation meaning, so a `$comment`-only delta is not drift. This was
        previously pinned as a deliberate residual (still-diverged); D1
        inverted that pin the other way. A reader hitting this test should see
        a decided question, not a flipped assertion with no context."""
        edited = dict(_CANONICAL_WIDGET)
        edited["$comment"] = "reworded comment, same schema semantics"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        assert result["determinate"] is True
        assert result["direction"] is None

    def test_ac2b_detail_names_comment_cause_not_formatting_only(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC5 applies to the AC2b path too: a `$comment`-only match must name
        the annotation-strip cause, not the "formatting-only delta" wording —
        the fixture and the edited vendored copy are both serialized with the
        same `indent=2, sort_keys=True`, so nothing about whitespace/key-order/
        trailing-newline actually differs here; only the `$comment` prose does.
        A message claiming "formatting-only" on this path misattributes the
        cause and defeats AC5's own stated purpose."""
        edited = dict(_CANONICAL_WIDGET)
        edited["$comment"] = "reworded comment, same schema semantics"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        detail_lower = result["detail"].lower()
        assert "$comment" in result["detail"] or "comment" in detail_lower
        assert "formatting-only delta" not in detail_lower

    def test_comment_key_dropped_but_comment_as_value_untouched(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """D1 negative-spec: `$comment` is dropped ONLY as a dict KEY. A string
        literally reading "$comment" that appears in VALUE position — here, as
        an array element on an unrelated key — is an ordinary leaf value and
        must still report diverged=True on edit, exactly like any other value
        change. This is the obvious way to get a recursive `$comment` strip
        subtly wrong (stripping by string equality instead of by key
        position), so it gets its own test rather than riding along on AC2b."""
        base = dict(_CANONICAL_WIDGET)
        base["tags"] = ["$comment", "widget"]

        doe_repo = tmp_path / "coordinator-claude-tagged"
        schemas = doe_repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps(base, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _advisory_git(doe_repo, "init", "-q")
        _advisory_git(doe_repo, "config", "user.email", "test@example.invalid")
        _advisory_git(doe_repo, "config", "user.name", "canonical drift advisory test")
        _advisory_git(doe_repo, "add", "-A")
        _advisory_git(doe_repo, "commit", "-q", "-m", "seed widget schema with $comment-as-value")

        edited = dict(base)
        edited["tags"] = ["widget", "other-tag"]
        vendored_dir = tmp_path / "vendored-edited"
        vendored_dir.mkdir()
        vendored_edited = vendored_dir / "widget.schema.json"
        vendored_edited.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        result = check_schema_drift_advisory(vendored_edited, doe_repo)
        assert result["determinate"] is True
        assert result["diverged"] is True

    def test_comment_delta_combined_with_real_value_change_still_diverges(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """D1 negative-spec: dropping `$comment` must not mask a genuine
        divergence sitting next to one. A vendored copy with BOTH a
        `$comment`-only edit AND an ordinary value edit still reports
        diverged=True — the `$comment` strip removes exactly one key, not the
        rest of the comparison."""
        edited = dict(_CANONICAL_WIDGET)
        edited["$comment"] = "reworded comment, same schema semantics"
        edited["title"] = "widget (locally edited)"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["direction"] is not None

    def test_ac3_malformed_vendored_json_falls_back_to_byte_diverged(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC3: vendored side fails to parse as JSON while coordinator-claude's side parses
        fine -> falls back to the raw byte test and reports diverged=True,
        without raising. A malformed vendored schema must never be normalized
        into looking clean."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text("not json at all {{{", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["determinate"] is True

    def test_ac3_malformed_doe_json_falls_back_to_byte_diverged(
        self, tmp_path: Path
    ) -> None:
        """AC3, symmetric case: coordinator-claude HEAD's side fails to parse as JSON while
        the vendored copy parses fine -> falls back to the raw byte test and
        reports diverged=True, without raising. AC3 and the docstring both say
        "either side" — this pins the side the existing vendored-malformed
        test doesn't cover, so a regression that special-cased only one side
        of the symmetric `doe_canonical is not None and local_canonical is not
        None` check would be caught here."""
        if not _which_git():
            pytest.skip("git not available")
        doe_repo = tmp_path / "coordinator-claude-malformed"
        schemas = doe_repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text("not json at all {{{", encoding="utf-8")
        _advisory_git(doe_repo, "init", "-q")
        _advisory_git(doe_repo, "config", "user.email", "test@example.invalid")
        _advisory_git(doe_repo, "config", "user.name", "canonical drift advisory test")
        _advisory_git(doe_repo, "add", "-A")
        _advisory_git(doe_repo, "commit", "-q", "-m", "seed malformed widget schema")

        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(_CANONICAL_WIDGET, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result = check_schema_drift_advisory(vendored, doe_repo)
        assert result["diverged"] is True
        assert result["determinate"] is True

    def test_ac3_both_sides_malformed_falls_back_to_byte_diverged(
        self, tmp_path: Path
    ) -> None:
        """AC3, both-malformed case: neither side parses as JSON -> falls back
        to the raw byte test. Byte-differing malformed texts still report
        diverged=True, determinate=True — the fallback never raises out of a
        double parse failure and never normalizes a malformed pair into
        looking clean."""
        if not _which_git():
            pytest.skip("git not available")
        doe_repo = tmp_path / "coordinator-claude-both-malformed"
        schemas = doe_repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text("not json at all {{{", encoding="utf-8")
        _advisory_git(doe_repo, "init", "-q")
        _advisory_git(doe_repo, "config", "user.email", "test@example.invalid")
        _advisory_git(doe_repo, "config", "user.name", "canonical drift advisory test")
        _advisory_git(doe_repo, "add", "-A")
        _advisory_git(doe_repo, "commit", "-q", "-m", "seed malformed widget schema")

        vendored = tmp_path / "widget.schema.json"
        vendored.write_text("also not json ]]]", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, doe_repo)
        assert result["diverged"] is True
        assert result["determinate"] is True

    def test_ac5_formatting_only_detail_names_canonical_comparison(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC5: the formatting-only-match `detail` string names the canonical
        comparison, so a reader isn't left wondering why a visibly-different
        file reports clean."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(_CANONICAL_WIDGET, indent=4, sort_keys=False) + "\n\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is False
        assert "canonical" in result["detail"].lower()

    def test_ac6_formatting_only_delta_leaves_version_and_bump_keys_intact(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC6: on the formatting-only match path, local_version/doe_version/
        local_bump_class/doe_bump_class/doe_bump_note still carry the values
        they carried before this change — the canonicalization only touches
        `diverged`/`determinate`/`direction`/`detail`."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(_CANONICAL_WIDGET, indent=4, sort_keys=False) + "\n\n",
            encoding="utf-8",
        )
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["local_version"] == "1.0.0"
        assert result["doe_version"] == "1.0.0"
        assert result["local_bump_class"] == "nested-field-additive"
        assert result["doe_bump_class"] == "nested-field-additive"
        assert result["doe_bump_note"] == "added an optional field"

    def test_ac4_gating_check_still_raises_on_whitespace_only_difference(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """AC4 — Anti-scope guard, not a duplicate of the advisory tests above:
        `check_schema_drift` (the gating tamper-check) stays byte-exact and
        MUST still raise SchemaDriftError on a whitespace-only difference,
        even though `check_schema_drift_advisory` above now reports that same
        class of delta as not-diverged. This test exists to stop a future
        session from "reconciling the inconsistency" between the two
        functions — the asymmetry is the design (see check_schema_drift_advisory's
        own negative-spec docstring and the plan's Anti-scope), not a bug."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(
            json.dumps(_CANONICAL_WIDGET, indent=4, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(SchemaDriftError, match="diverges"):
            check_schema_drift(vendored, fake_doe)


class TestDivergenceKind:
    """`divergence_kind` on check_schema_drift_advisory — the axis orthogonal to
    `direction` distinguishing a validation-SHAPE delta from a PROSE-only one
    (description/$comment/x-bump-note), via schema_shape.semantic_shape_hash.

    Spec backlink: 2026-08-13 parity-tail exchange (coordinator-claude-EM: "'reconcile by hand'
    on a punctuation diff trains people to stop reading it" — 10 of 12
    then-drifted schemas carried DIRECTION_BOTH's reconcile-by-hand prose despite
    byte-identical validation shape).
    """

    @pytest.fixture()
    def fake_doe(self, tmp_path: Path) -> Path:
        if not _which_git():
            pytest.skip("git not available")
        repo = tmp_path / "coordinator-claude-fake"
        schemas = repo / "coordinator" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "widget.schema.json").write_text(
            json.dumps(_CANONICAL_WIDGET, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _advisory_git(repo, "init", "-q")
        _advisory_git(repo, "config", "user.email", "test@example.invalid")
        _advisory_git(repo, "config", "user.name", "divergence kind test")
        _advisory_git(repo, "add", "-A")
        _advisory_git(repo, "commit", "-q", "-m", "seed widget schema")
        return repo

    def test_description_only_delta_reports_prose_only(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """A `description` edit changes nothing schema_shape hashes over ->
        divergence_kind == 'prose-only', even though the byte/canonical
        comparison above still reports diverged=True (description is not
        `$comment`, so D1's canonicalization doesn't absorb it)."""
        edited = dict(_CANONICAL_WIDGET)
        edited["description"] = "a longer, more detailed description of widget"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["divergence_kind"] == "prose-only"

    def test_required_field_delta_reports_shape(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """A `required` list edit changes validation behaviour ->
        divergence_kind == 'shape'."""
        edited = dict(_CANONICAL_WIDGET)
        required = list(edited.get("required", []))
        required.append("newly_required_field")
        edited["required"] = required
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["divergence_kind"] == "shape"

    def test_malformed_vendored_json_reports_divergence_kind_none(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """Malformed vendored JSON falls back to the text-containment path,
        which has no parsed structure to hash -> divergence_kind is None,
        never guessed."""
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text("not json at all {{{", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        assert result["diverged"] is True
        assert result["divergence_kind"] is None

    def test_prose_only_detail_still_warns_about_losing_local_prose(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        """The prose-only `detail` string states the kind explicitly, says no
        validation behaviour differs, and STILL warns that a blind re-vendor
        drops claude-klabauter's prose — the risk is real (regressed once, commit
        1825e7771) and the new axis must not read as a downgrade."""
        edited = dict(_CANONICAL_WIDGET)
        edited["description"] = "a longer, more detailed description of widget"
        vendored = tmp_path / "widget.schema.json"
        vendored.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = check_schema_drift_advisory(vendored, fake_doe)
        detail_lower = result["detail"].lower()
        assert "prose-only" in detail_lower
        assert "no validator" in detail_lower or "validation shape is identical" in detail_lower
        assert "prose" in detail_lower
        assert "re-vendor" in detail_lower


class TestPinnedQueueSchemaDrift:
    """Gating tamper-check: each vendored queue schema must still equal the pin.

    One test method per schema (not parametrized/aggregate) — a single schema's
    drift must not be maskable by the other seven passing.
    """

    def test_bug_backlog_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'bug-backlog.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['bug-backlog'],
        )

    def test_cross_repo_commitment_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'cross-repo-commitment.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['cross-repo-commitment'],
        )

    def test_debt_backlog_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'debt-backlog.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['debt-backlog'],
        )

    def test_improvement_queue_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'improvement-queue.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['improvement-queue'],
        )

    def test_lesson_entry_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'lesson-entry.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['lesson-entry'],
        )

    def test_lessons_outbox_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'lessons-outbox.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['lessons-outbox'],
        )

    def test_review_findings_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'review-findings.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['review-findings'],
        )

    def test_review_trail_matches_pinned_sha(self):
        # Was an ahead-pin for a few hours on 2026-08-10 while claude-klabauter led coordinator-claude
        # on the scope_kind enum close. Coordinator-claude vendored it themselves at their
        # 6baac04a3 ("review-trail: adopt the scope_kind enum from the
        # vendored side"), which is exactly the condition
        # check_schema_ahead_of_doe's stale-ahead branch exists to detect --
        # and it did detect it, unprompted, within hours of landing. The
        # convergence was then taken through bin/claude-klabauter-revendor-schema.py,
        # so this is an ordinary byte-pin again and the ahead-pin entry is
        # gone. Nothing about that is a special case worth preserving here.
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'review-trail.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['review-trail'],
        )

    def test_priority_ledger_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'priority-ledger.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['priority-ledger'],
        )

    def test_priority_intent_matches_pinned_sha(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        check_schema_drift(
            _SCHEMAS_DIR / 'priority-intent.schema.json',
            _DOE_REPO,
            ref=_QUEUE_SCHEMA_PINS['priority-intent'],
        )


class TestAheadPinRegistryRouting:
    """The real consumer of `_QUEUE_SCHEMA_AHEAD_PINS` — routes every entry
    through `check_schema_ahead_of_doe` against the live coordinator-claude clone. Trivially
    green while the registry is empty (the healthy resting state), but the
    moment an entry is added it is ACTUALLY exercised, closing the gap
    P1-3 identified: the registry used to be read by nothing, so an added
    entry would silently fail to gate.

    Review: eng-director P1-3.
    """

    def test_every_ahead_pin_entry_is_routed_and_passes(self):
        if _DOE_REPO is None or not _DOE_REPO.exists():
            pytest.skip(f'coordinator-claude repo not found at {_DOE_REPO}')
        if not _QUEUE_SCHEMA_AHEAD_PINS:
            pytest.skip('ahead-pin registry is empty — the healthy resting state')
        for name, entry in _QUEUE_SCHEMA_AHEAD_PINS.items():
            check_schema_ahead_of_doe(
                _SCHEMAS_DIR / f'{name}.schema.json',
                _DOE_REPO,
                doe_ref=entry['doe_ref'],
                reason=entry['reason'],
                provenance=entry['provenance'],
                exempt_paths=entry.get('exempt_paths', frozenset()),
                local_shape_hash=entry.get('local_shape_hash'),
            )


def _ahead_git(repo: Path, *args: str, env: dict | None = None) -> None:
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    subprocess.run(
        ['git', '-C', str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=run_env,
    )


class TestCheckSchemaAheadOfDoe:
    """Direct unit coverage for check_schema_ahead_of_doe's four checks, over a
    throwaway tmp_path git repo — same fixture discipline as
    TestAdvisoryLocalDoeVersions above. Closes the "~199 lines with zero
    tests" gap: one test per failure branch plus the green path.

    Review: eng-director P1-3.
    """

    @pytest.fixture()
    def fake_doe(self, tmp_path: Path):
        if not _which_git():
            pytest.skip("git not available")

        def _make(version: str = "1.0.0", extra: dict | None = None) -> Path:
            repo = tmp_path / f"coordinator-claude-fake-{version.replace('.', '_')}"
            schemas = repo / "coordinator" / "schemas"
            schemas.mkdir(parents=True)
            body = {"x-schema-version": version, "title": "widget"}
            if extra:
                body.update(extra)
            (schemas / "widget.schema.json").write_text(
                json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            _ahead_git(repo, "init", "-q")
            _ahead_git(repo, "config", "user.email", "test@example.invalid")
            _ahead_git(repo, "config", "user.name", "ahead-pin test")
            _ahead_git(repo, "add", "-A")
            _ahead_git(repo, "commit", "-q", "-m", f"seed widget schema {version}")
            return repo

        return _make

    def _local(self, tmp_path: Path, version: str = "1.1.0", extra: dict | None = None) -> Path:
        body = {"x-schema-version": version, "title": "widget"}
        if extra:
            body.update(extra)
        local = tmp_path / "widget.schema.json"
        local.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return local

    def test_green_path_ahead_version_passes(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0")
        local = self._local(tmp_path, "1.1.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        check_schema_ahead_of_doe(
            local, repo, doe_ref=doe_ref, reason="test", provenance="test",
        )

    def test_stale_ahead_raises_when_doe_moved(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0")
        stale_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # coordinator-claude moves after the pin was recorded.
        schemas = repo / "coordinator" / "schemas"
        (schemas / "widget.schema.json").write_text(
            json.dumps({"x-schema-version": "1.1.0", "title": "widget"}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _ahead_git(repo, "add", "-A")
        _ahead_git(repo, "commit", "-q", "-m", "coordinator-claude moved")
        local = self._local(tmp_path, "1.1.0")
        with pytest.raises(SchemaDriftError, match="STALE"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=stale_ref, reason="test", provenance="test",
            )

    def test_stale_ahead_raises_when_doe_moved_on_a_different_branch(self, fake_doe, tmp_path: Path) -> None:
        """Review: code-reviewer P3 -- prior coverage only moved coordinator-claude on the
        SAME branch. Check 1 explicitly resolves against `git log --all`
        (any local ref), not `HEAD`; this exercises that a second branch
        moving the schema is caught too, not just more commits on the
        checked-out one."""
        repo = fake_doe("1.0.0")
        stale_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _ahead_git(repo, "checkout", "-q", "-b", "other-branch")
        schemas = repo / "coordinator" / "schemas"
        (schemas / "widget.schema.json").write_text(
            json.dumps({"x-schema-version": "1.1.0", "title": "widget"}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _ahead_git(repo, "add", "--", "coordinator/schemas/widget.schema.json")
        # Pinned committer date strictly after the seed commit -- `git log
        # --all` orders by commit date, and two commits landing in the same
        # wall-clock second (routine under fast test execution) makes the
        # "most recent" result ref-declaration-order-dependent rather than
        # deterministic, which would make this assertion flaky.
        _ahead_git(
            repo, "commit", "-q", "-m", "coordinator-claude moved on another branch",
            env={"GIT_AUTHOR_DATE": "2030-01-02T00:00:00", "GIT_COMMITTER_DATE": "2030-01-02T00:00:00"},
        )
        # Back on the original branch, which never saw this commit.
        _ahead_git(repo, "checkout", "-q", "-")
        local = self._local(tmp_path, "1.1.0")
        with pytest.raises(SchemaDriftError, match="STALE"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=stale_ref, reason="test", provenance="test",
            )

    def test_leaf_not_retained_raises(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0", extra={"applies_to": "state/review-trail/*.json"})
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Real divergence, not extension: the leaf value CHANGES, not merely
        # gains a trailing char in a description field — must not pass.
        local = self._local(tmp_path, "1.1.0", extra={"applies_to": "state/review-trail/*.jsonl"})
        with pytest.raises(SchemaDriftError, match="leaf-retention"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )

    def test_description_append_is_retained(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0", extra={"description": "short"})
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0", extra={"description": "short, extended"})
        check_schema_ahead_of_doe(
            local, repo, doe_ref=doe_ref, reason="test", provenance="test",
        )

    def test_bump_note_append_is_retained(self, fake_doe, tmp_path: Path) -> None:
        """Review: code-reviewer P1 -- x-bump-note is prose-append, same shape
        as `description`. Pins the regression: the predicate used to check
        only `path[-1] == 'description'`, so an ahead-bump's own bump-note
        append (the exact case the module comment calls out) failed the
        gate on the normal case, not an edge case."""
        repo = fake_doe("1.0.0", extra={"x-bump-note": "added an optional field"})
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(
            tmp_path, "1.1.0",
            extra={"x-bump-note": "added an optional field; also widened an enum"},
        )
        check_schema_ahead_of_doe(
            local, repo, doe_ref=doe_ref, reason="test", provenance="test",
        )

    def test_bump_note_narrowing_still_raises(self, fake_doe, tmp_path: Path) -> None:
        """The prose carve-out is append-only, not free-form rewrite tolerance:
        a bump-note whose text is NOT a superstring of coordinator-claude's must still fail."""
        repo = fake_doe("1.0.0", extra={"x-bump-note": "added an optional field"})
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0", extra={"x-bump-note": "renamed a field"})
        with pytest.raises(SchemaDriftError, match="leaf-retention"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )

    def test_exempt_paths_kwarg_honors_caller_supplied_exemption(self, fake_doe, tmp_path: Path) -> None:
        """Review: code-reviewer P2 -- `exempt_paths` had no direct-call
        coverage; the registry-routing test never exercises a non-default
        value. Proves a caller-supplied path is actually excluded from the
        retention check, not merely unioned by inspection."""
        repo = fake_doe("1.0.0", extra={"applies_to": "state/review-trail/*.json"})
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0", extra={"applies_to": "state/review-trail/*.jsonl"})
        with pytest.raises(SchemaDriftError, match="leaf-retention"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )
        # Same divergent leaf, now exempted per-call -- must pass.
        check_schema_ahead_of_doe(
            local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            exempt_paths=frozenset({("applies_to",)}),
        )

    def test_version_not_strictly_greater_raises(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.1.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0")
        with pytest.raises(SchemaDriftError, match="strictly greater"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )

    def test_dirty_doe_worktree_refuses_rather_than_passes(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Uncommitted edit on the coordinator-claude side — must be refused, not read as "unmoved".
        (repo / "coordinator" / "schemas" / "widget.schema.json").write_text(
            json.dumps({"x-schema-version": "1.0.0", "title": "widget (dirty)"}, indent=2) + "\n",
            encoding="utf-8",
        )
        local = self._local(tmp_path, "1.1.0")
        with pytest.raises(SchemaDriftError, match="uncommitted"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )

    def test_local_shape_hash_mismatch_raises(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0")
        with pytest.raises(SchemaDriftError, match="tamper-pin"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
                local_shape_hash="0" * 64,
            )

    def test_local_shape_hash_match_passes(self, fake_doe, tmp_path: Path) -> None:
        from coordinator_core.frontmatter.schema_validate import _local_shape_hash

        repo = fake_doe("1.0.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "1.1.0")
        expected = _local_shape_hash(local.read_text(encoding="utf-8"))
        check_schema_ahead_of_doe(
            local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            local_shape_hash=expected,
        )

    def test_unparseable_semver_fails_closed(self, fake_doe, tmp_path: Path) -> None:
        repo = fake_doe("1.0.0")
        doe_ref = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        local = self._local(tmp_path, "not-a-version")
        with pytest.raises(SchemaDriftError, match="could not compare"):
            check_schema_ahead_of_doe(
                local, repo, doe_ref=doe_ref, reason="test", provenance="test",
            )


class TestParseSemverTuple:
    """Direct unit coverage for _parse_semver_tuple, the ahead-pin's version
    comparator. Review: eng-director P1-3 (was untested)."""

    def test_well_formed_triple(self) -> None:
        assert _parse_semver_tuple("1.2.3") == (1, 2, 3)

    def test_garbage_returns_none(self) -> None:
        assert _parse_semver_tuple("not-a-version") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_semver_tuple("") is None


# ---------------------------------------------------------------------------
# Round-trip: primitives mutation → YAML parse → validate_frontmatter
#
# These tests exercise the post-mutation validation path that handoff.transition
# uses. They build realistic handoff documents, mutate them via primitives, then
# re-parse to a dict and validate with validate_frontmatter.
# ---------------------------------------------------------------------------

def _parse_fm_text_to_dict(fm_text: str) -> dict:
    """Parse frontmatter YAML text into a dict using PyYAML safe_load."""
    return yaml.safe_load(fm_text) or {}


class TestRoundTripValidation:
    def _make_handoff_doc(self, **fields) -> str:
        """Build a minimal handoff document string."""
        fm_lines = []
        for key, value in fields.items():
            if isinstance(value, bool):
                fm_lines.append(f'{key}: {str(value).lower()}')
            elif value is None:
                fm_lines.append(f'{key}: null')
            elif isinstance(value, list):
                if not value:
                    fm_lines.append(f'{key}: []')
                else:
                    fm_lines.append(f'{key}:')
                    for item in value:
                        # Review: code-reviewer — F12: quote items via serialize_yaml_scalar so
                        # structural chars (:, #, {, etc.) don't produce malformed YAML.
                        fm_lines.append(f'  - {serialize_yaml_scalar(str(item))}')
            else:
                fm_lines.append(f'{key}: {value}')
        fm_text = '\n'.join(fm_lines) + '\n'
        return f'---\n{fm_text}---\n# Body\n'

    def test_valid_handoff_after_claim_transition(self):
        """Simulate the claim transition: status→claimed, insert claimed_at + claimed_by."""
        doc = self._make_handoff_doc(
            title='Test handoff',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='open',
            predecessor='state/handoffs/2026-07-04.md',
            kind='session-handoff',
            category='infra',
            summary='A short valid summary for this test handoff',
            deployment_state='ready_to_fire',
            pickup_ready=True,
        )
        split = split_frontmatter(doc)
        assert split is not None

        fm = split.fm_text
        fm = replace_fm_field(fm, 'status', 'claimed')
        fm = replace_fm_field(fm, 'deployment_state', 'in_flight')
        fm = insert_fm_field(fm, 'claimed_at', '2026-07-05', after_key='deployment_state')
        fm = insert_fm_field(fm, 'claimed_by', 'sess-abc123', after_key='claimed_at')

        rebuilt = rebuild(split, fm)
        out = split_frontmatter(rebuilt)
        assert out is not None

        fm_dict = _parse_fm_text_to_dict(out.fm_text)
        errors = validate_frontmatter(fm_dict, _HANDOFF_SCHEMA)
        assert errors == [], f'Unexpected errors after claim transition: {errors}'

    def test_over_140_char_summary_fails_post_mutation(self):
        """The canonical post-mutation gate: summary exceeds 140 chars → rejected."""
        long_summary = 'A' * 141  # 141 chars — just over the limit
        doc = self._make_handoff_doc(
            title='Test',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='open',
            predecessor='state/handoffs/prev.md',
            kind='session-handoff',
            category='infra',
            summary=long_summary,
        )
        split = split_frontmatter(doc)
        assert split is not None
        fm_dict = _parse_fm_text_to_dict(split.fm_text)
        errors = validate_frontmatter(fm_dict, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'summary' and '141' in e['error'] for e in errors)

    def test_exactly_140_char_summary_passes_post_mutation(self):
        """Summary of exactly 140 chars is the boundary — must pass."""
        exact_summary = 'B' * 140
        doc = self._make_handoff_doc(
            title='Test',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='open',
            predecessor='state/handoffs/prev.md',
            kind='session-handoff',
            category='infra',
            summary=exact_summary,
        )
        split = split_frontmatter(doc)
        assert split is not None
        fm_dict = _parse_fm_text_to_dict(split.fm_text)
        errors = validate_frontmatter(fm_dict, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'summary' for e in errors)

    def test_insert_category_then_validate(self):
        """Simulate the normalize flow: insert category field, then validate."""
        doc = self._make_handoff_doc(
            title='Test handoff',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='open',
            predecessor='state/handoffs/prev.md',
            kind='session-handoff',
            summary='Short summary',
        )
        split = split_frontmatter(doc)
        assert split is not None

        fm = split.fm_text
        # Simulate normalize inserting category
        if read_fm_field(fm, 'category') is None:
            fm = insert_fm_field(fm, 'category', 'infra')

        rebuilt = rebuild(split, fm)
        out = split_frontmatter(rebuilt)
        fm_dict = _parse_fm_text_to_dict(out.fm_text)

        errors = validate_frontmatter(fm_dict, _HANDOFF_SCHEMA)
        assert errors == [], f'Unexpected errors after normalize insert: {errors}'

    def test_missing_category_after_mutation_caught(self):
        """If category is never inserted (missing on post-cutoff doc), validator catches it."""
        doc = self._make_handoff_doc(
            title='Test',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='open',
            predecessor='state/handoffs/prev.md',
            kind='session-handoff',
            summary='Short summary',
            # category intentionally absent
        )
        split = split_frontmatter(doc)
        fm_dict = _parse_fm_text_to_dict(split.fm_text)
        errors = validate_frontmatter(fm_dict, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'category' for e in errors)

    def test_archived_handoff_round_trip_no_cross_field_errors(self):
        """Archived handoff with deployment_state=awaiting_gate: no cross-field errors."""
        doc = self._make_handoff_doc(
            title='Archived',
            created='2026-07-05',
            branch='work/test/2026-07-05',
            status='claimed',
            predecessor='state/handoffs/prev.md',
            deployment_state='awaiting_gate',  # no gate_dependency — would fail active schema
        )
        split = split_frontmatter(doc)
        fm_dict = _parse_fm_text_to_dict(split.fm_text)
        errors = validate_frontmatter(fm_dict, _HANDOFF_ARCHIVED_SCHEMA)
        assert not any(e['field'] == 'gate_dependency' for e in errors)


# ---------------------------------------------------------------------------
# Memo cross-field rules — validate_memo_cross_fields
#
# Port of CROSS_FIELD_RULES['cross-repo-memo'] from coordinator-claude coordinator/bin/lib/schema.js:1332-1522.
# ---------------------------------------------------------------------------


def _valid_memo(**overrides) -> dict:
    """Minimal valid post-cutoff memo dict (all cross-field rules fire)."""
    base = {
        'title': 'Test memo',
        'created': '2026-06-01',
        'status': 'open',
        'from': 'test-em',
    }
    base.update(overrides)
    return base


class TestMemoValidatorHappyPath:
    def test_minimal_open_memo_valid(self):
        errors = validate_memo_cross_fields(_valid_memo())
        assert errors == []

    def test_in_progress_with_picked_up_by_valid(self):
        fm = _valid_memo(status='in_progress', picked_up_by='sess-abc123')
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_actioned_decision_accepted_with_path_realized_by_valid(self):
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='docs/plans/2026-06-01-foo.md')
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_actioned_decision_accepted_with_inline_realized_by_valid(self):
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='inline')
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_actioned_decision_accepted_with_sha_realized_by_valid(self):
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='abc1234')
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_actioned_decision_declined_no_realized_by_valid(self):
        """decision=declined is exempt from realized_by requirement."""
        fm = _valid_memo(status='actioned', decision='declined')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'realized_by' for e in errors)

    def test_kind_absent_valid(self):
        fm = _valid_memo()
        assert 'kind' not in fm
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_kind_null_valid(self):
        fm = _valid_memo(kind=None)
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_kind_fyi_valid(self):
        fm = _valid_memo(kind='fyi')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_summary_at_cap_valid(self):
        fm = _valid_memo(summary='x' * 120)
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'summary' for e in errors)

    def test_central_only_with_to_valid(self):
        fm = _valid_memo(delivery_mode='central-only', to='doe-em')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'to' for e in errors)


class TestMemoGrandfatherSkip:
    """Pre-cutoff memos (created < 2026-05-22) skip ALL cross-field checks."""

    def test_pre_cutoff_skip_returns_no_errors(self):
        """A memo with created < 2026-05-22 passes validation regardless of fields."""
        fm = {
            'created': '2026-05-01',
            'status': 'in_progress',
            # No picked_up_by — would fail for post-cutoff in_progress
        }
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_pre_cutoff_exact_boundary_skips(self):
        """created = 2026-05-21 (one day before cutoff) is still grandfathered."""
        fm = {
            'created': '2026-05-21',
            'status': 'actioned',
            'decision': 'accepted',
            # No realized_by — would fail for post-cutoff
        }
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_post_cutoff_exact_boundary_does_not_skip(self):
        """created = 2026-05-22 is NOT grandfathered — rules apply."""
        fm = {
            'created': '2026-05-22',
            'status': 'in_progress',
            # No picked_up_by — must fail
        }
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'picked_up_by' for e in errors)

    def test_no_created_field_does_not_skip(self):
        """Absent created field: grandfather rule returns None (no skip), rules apply."""
        fm = {
            'status': 'in_progress',
            # No created, no picked_up_by
        }
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'picked_up_by' for e in errors)


class TestMemoBareDateCoercion:
    """PyYAML safe_load coerces bare 'created: 2026-05-22' to datetime.date.

    The validator must coerce before comparing strings — a date < str raises TypeError.
    """

    def test_bare_date_created_coerced_post_cutoff(self):
        """datetime.date created value is coerced to ISO string before comparison."""
        fm = {
            'created': datetime.date(2026, 6, 1),  # PyYAML would yield this from bare date
            'status': 'in_progress',
            # No picked_up_by — should fail (post-cutoff, rules apply after coercion)
        }
        # Must not raise TypeError — coercion handles datetime.date < str comparison
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'picked_up_by' for e in errors)

    def test_bare_date_created_coerced_pre_cutoff_skips(self):
        """datetime.date pre-cutoff value is coerced then grandfather fires → skip."""
        fm = {
            'created': datetime.date(2026, 5, 1),  # pre-cutoff as datetime.date
            'status': 'in_progress',
        }
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_datetime_created_coerced(self):
        """datetime.datetime created value is coerced to ISO string (handles TZ-naive)."""
        fm = {
            'created': datetime.datetime(2026, 6, 1, 12, 0, 0),
            'status': 'open',
        }
        errors = validate_memo_cross_fields(fm)
        assert errors == []


class TestMemoRuleInProgressNeedsPickedUpBy:
    def test_in_progress_without_picked_up_by_fails(self):
        fm = _valid_memo(status='in_progress')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'picked_up_by' and 'in_progress' in e['error'] for e in errors)

    def test_in_progress_with_empty_picked_up_by_fails(self):
        fm = _valid_memo(status='in_progress', picked_up_by='   ')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'picked_up_by' for e in errors)

    def test_open_status_no_picked_up_by_ok(self):
        fm = _valid_memo(status='open')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'picked_up_by' for e in errors)


class TestMemoRuleActionedRequiresRealizedBy:
    def test_actioned_accepted_missing_realized_by_fails(self):
        fm = _valid_memo(status='actioned', decision='accepted')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'realized_by' and 'required' in e['error'] for e in errors)

    def test_actioned_partial_missing_realized_by_fails(self):
        fm = _valid_memo(status='actioned', decision='partial')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'realized_by' and 'required' in e['error'] for e in errors)

    def test_actioned_accepted_malformed_realized_by_fails(self):
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='bareword')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'realized_by' and 'malformed' in e['error'] for e in errors)

    def test_actioned_accepted_sha_realized_by_valid(self):
        """7-char hex SHA is well-formed."""
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='abc1234')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'realized_by' for e in errors)

    def test_actioned_accepted_long_sha_valid(self):
        """40-char hex SHA (full git SHA) is well-formed."""
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='a' * 40)
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'realized_by' for e in errors)

    def test_actioned_accepted_uppercase_hex_sha_valid(self):
        """Uppercase hex SHA is well-formed (case-insensitive per oracle F1 review note)."""
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='ABC1234')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'realized_by' for e in errors)

    def test_actioned_fyi_no_decision_no_realized_by_ok(self):
        """decision=fyi (absent from accepted/partial) — realized_by not required."""
        fm = _valid_memo(status='actioned', decision='fyi')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'realized_by' for e in errors)


class TestMemoRuleActionTakenRequiresCompanions:
    def test_action_taken_missing_both_fails(self):
        # Review: code-reviewer — F2: assert both missing companion field names are named
        # in the error field, so a regression that flags only one of them still fails.
        fm = _valid_memo(status='action_taken')
        errors = validate_memo_cross_fields(fm)
        assert any('action_taken_at' in e['field'] for e in errors)
        assert any('decision' in e['field'] for e in errors)

    def test_action_taken_missing_action_taken_at_fails(self):
        fm = _valid_memo(status='action_taken', decision='accepted')
        errors = validate_memo_cross_fields(fm)
        assert any('action_taken_at' in e['field'] for e in errors)

    def test_action_taken_missing_decision_fails(self):
        fm = _valid_memo(status='action_taken', action_taken_at='2026-06-01T10:00:00Z')
        errors = validate_memo_cross_fields(fm)
        assert any('decision' in e['field'] for e in errors)

    def test_action_taken_complete_ok(self):
        fm = _valid_memo(
            status='action_taken',
            action_taken_at='2026-06-01T10:00:00Z',
            decision='accepted',
        )
        errors = validate_memo_cross_fields(fm)
        assert not any('action_taken_at' in e['field'] or 'decision' in e['field'] for e in errors)


class TestMemoRuleClosedRequiresCompanions:
    def test_closed_missing_all_fails(self):
        # Review: code-reviewer — F2: assert all three missing companion field names are
        # named in the errors, so a regression flagging only one or the wrong field fails.
        fm = _valid_memo(status='closed')
        errors = validate_memo_cross_fields(fm)
        assert any('closed_at' in e['field'] for e in errors)
        assert any('action_taken_at' in e['field'] for e in errors)
        assert any('decision' in e['field'] for e in errors)

    def test_closed_complete_ok(self):
        fm = _valid_memo(
            status='closed',
            closed_at='2026-06-02T10:00:00Z',
            action_taken_at='2026-06-01T10:00:00Z',
            decision='accepted',
        )
        errors = validate_memo_cross_fields(fm)
        assert not any('closed_at' in e['field'] or 'action_taken_at' in e['field'] for e in errors)


class TestMemoRuleSupersededRequiresSupersededBy:
    def test_superseded_without_superseded_by_fails(self):
        fm = _valid_memo(status='superseded')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'superseded_by' for e in errors)

    def test_superseded_with_superseded_by_ok(self):
        fm = _valid_memo(status='superseded', superseded_by='cross-repo/inbox/2026-06-01-new.md')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'superseded_by' for e in errors)


class TestMemoRuleCentralOnlyRequiresTo:
    def test_central_only_without_to_fails(self):
        fm = _valid_memo(delivery_mode='central-only')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'to' and 'central-only' in e['error'] for e in errors)

    def test_central_only_with_empty_to_fails(self):
        fm = _valid_memo(delivery_mode='central-only', to='')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'to' for e in errors)

    def test_other_delivery_mode_no_to_ok(self):
        fm = _valid_memo(delivery_mode='routed')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'to' for e in errors)

    def test_absent_delivery_mode_no_to_ok(self):
        fm = _valid_memo()
        assert 'delivery_mode' not in fm
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'to' for e in errors)


class TestMemoRuleSummaryLengthCap:
    def test_summary_at_120_chars_valid(self):
        fm = _valid_memo(summary='x' * 120)
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'summary' for e in errors)

    def test_summary_at_121_chars_fails(self):
        fm = _valid_memo(summary='x' * 121)
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'summary' and '120' in e['error'] for e in errors)

    def test_summary_over_140_would_fail_memo_cap(self):
        """Even values that would pass handoff's 140-char cap fail memo's 120-char cap."""
        fm = _valid_memo(summary='x' * 130)
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'summary' for e in errors)

    def test_summary_absent_ok(self):
        fm = _valid_memo()
        assert 'summary' not in fm
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'summary' for e in errors)


class TestMemoRuleKindEnum:
    def test_valid_kind_ask(self):
        fm = _valid_memo(kind='ask')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_valid_kind_consult(self):
        fm = _valid_memo(kind='consult')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_valid_kind_fyi(self):
        fm = _valid_memo(kind='fyi')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_valid_kind_proposal(self):
        fm = _valid_memo(kind='proposal')
        errors = validate_memo_cross_fields(fm)
        assert not any(e['field'] == 'kind' for e in errors)

    def test_invalid_kind_ack_fails(self):
        """'ack' is NOT a valid kind — acknowledgement is receipt-state."""
        fm = _valid_memo(kind='ack')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'kind' and 'ack' in e['error'] for e in errors)

    def test_invalid_kind_arbitrary_fails(self):
        fm = _valid_memo(kind='unknown-kind')
        errors = validate_memo_cross_fields(fm)
        assert any(e['field'] == 'kind' for e in errors)


class TestMemoRuleDispositionSupersededRequiresCompanions:
    """disposition_superseded=true requires superseding_note/superseding_realized_by/
    superseded_at + status already actioned/superseded — the append-only
    supersede-disposition mechanism's presence-triggered completeness gate."""

    def test_absent_disposition_superseded_ok(self):
        fm = _valid_memo(status='actioned', decision='accepted', realized_by='inline')
        errors = validate_memo_cross_fields(fm)
        assert not any('superseding' in e['field'] or 'superseded_at' in e['field'] for e in errors)

    def test_disposition_superseded_missing_all_companions_fails(self):
        fm = _valid_memo(
            status='actioned', decision='accepted', realized_by='inline',
            disposition_superseded=True,
        )
        errors = validate_memo_cross_fields(fm)
        assert any('superseding_note' in e['field'] for e in errors)
        assert any('superseding_realized_by' in e['field'] for e in errors)
        assert any('superseded_at' in e['field'] for e in errors)

    def test_disposition_superseded_complete_ok(self):
        fm = _valid_memo(
            status='actioned', decision='accepted', realized_by='inline',
            disposition_superseded=True,
            superseding_note='deny removed rather than re-messaged',
            superseding_realized_by='5fcece54e172',
            superseded_at='2026-08-12T16:00:00Z',
        )
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_disposition_superseded_true_but_status_open_fails(self):
        """Can't supersede a disposition that was never made."""
        fm = _valid_memo(
            status='open',
            disposition_superseded=True,
            superseding_note='n',
            superseding_realized_by='r',
            superseded_at='2026-08-12T16:00:00Z',
        )
        errors = validate_memo_cross_fields(fm)
        assert any('status' in e['field'] for e in errors)

    def test_disposition_superseded_falsy_ok_even_without_companions(self):
        fm = _valid_memo(status='open', disposition_superseded=False)
        errors = validate_memo_cross_fields(fm)
        assert not any('superseding' in e['field'] for e in errors)


# Review: code-reviewer (2026-07-24 D1 slice, P1) — parse_yaml() (the legacy-dialect
# YAML *parser*, distinct from _validate_legacy_field/_validate_legacy_yaml_frontmatter
# below which exercise the field *validator* against hand-built dicts) lost its ONLY
# standalone coverage when TestLegacyYamlDataLayerGoldenDifferential's node-oracle
# comparison was deleted in D1. Per the plan guardrail ("if a parity test is the
# port's ONLY standalone coverage, rewrite as pure-Python rather than delete"), this
# rewrites the two fixture cases as direct pure-Python assertions against a committed
# expected dict (seeded one-time from the retired node-oracle golden,
# schema_validate_node_oracle/parse_yaml_fixture_*.json at 480ad8f8^), dropping only
# the node comparison — coverage of parse_yaml() itself is preserved.
class TestParseYamlLegacyDialect:
    """Pure-Python coverage of parse_yaml() over the two committed legacy-YAML
    fixtures (nested mapping + inline `[a, b]` list; `>` block scalar interleaved
    with comments) — the two highest-complexity constructs the retired node-oracle
    differential used to exercise."""

    _LEGACY_YAML_FIXTURES_DIR = Path(__file__).parent / 'fixtures' / 'legacy_yaml_schemas'

    def test_parse_yaml_bug_backlog_nested_mapping_and_inline_list(self):
        text = (self._LEGACY_YAML_FIXTURES_DIR / 'bug-backlog.yaml').read_text(encoding='utf-8')
        expected = {
            'schema': 'bug-backlog',
            'applies_to': 'state/bug-backlog/*.yaml',
            'match_mode': 'whole-document-yaml',
            'required': {
                'created': 'iso-date',
                'title': 'string',
                'body': 'string',
                'status': {'type': 'enum', 'values': ['open', 'closed', 'deferred', 'wontfix']},
                'surface': 'string',
                'severity': {'type': 'enum', 'values': ['P0', 'P1', 'P2', 'P3']},
            },
            'optional': {
                'from_repo': 'string',
                'proposed_action': 'string',
                'closed_at': 'iso-date',
                'closed_by': 'string',
                'tags': 'list-of-string',
                'evidence': 'string',
                'repro_steps': 'string',
                'environment': 'string',
                'why_blocked': 'string',
                'initiative': {'type': 'string-or-null'},
                'system': {
                    'created_by_session': 'string',
                    'created_by_agent': 'string',
                    'linked_sessions': 'list-of-string',
                    'linked_commits': 'list-of-string',
                    'provenance_completeness': {'type': 'enum', 'values': ['complete', 'unknown']},
                },
            },
        }
        assert parse_yaml(text) == expected

    def test_parse_yaml_review_findings_block_scalar_with_comments(self):
        text = (self._LEGACY_YAML_FIXTURES_DIR / 'review-findings.yaml').read_text(encoding='utf-8')
        expected = {
            'schema': 'review-findings',
            'applies_to': 'state/review-trail/findings/*.md',
            'match_mode': 'no-frontmatter',
            'description': (
                'Pre-scaffolded code-reviewer self-persist findings sidecar. Each file is scoped to one\n'
                'code-reviewer dispatch (one review slice). The reviewer scaffolds its own sidecar here,\n'
                'replaces the FINDINGS sentinel with its full findings body via a single Edit, then returns\n'
                'a path pointer to the EM. The EM never absorbs the findings body into its own context.\n'
                'Kept in a dedicated findings/ subdir to preserve the json-only state/review-trail/ root.\n'
                'Spec backlink: cross-repo/inbox/2026-07-01-reviewer-selfpersist-confinement-redirect.md'
            ),
        }
        assert parse_yaml(text) == expected

    # Spec backlink: cross-repo/inbox/2026-08-06-coordinator-claude-em-sizing-advisory-latch-
    # all-three-taken.md — coordinator-claude-em reported the advisory inventing property names
    # off a sizing-object that parses clean under yaml.safe_load. A block scalar opened
    # ON a list item's dash line had its body parsed as a sibling mapping, so any colon
    # in the body minted a key. Negative-spec: the body is a scalar, never a mapping —
    # a colon inside it is content, not a key separator.
    def test_block_scalar_on_dash_line_keeps_its_body_and_invents_no_keys(self):
        text = (
            'probes:\n'
            '  - rationale: >-\n'
            '      The ask reverses a prior\n'
            '      decision in three places (plan/SKILL.md, hook): each re-derives it.\n'
            '    route: plan\n'
        )
        parsed = parse_yaml(text)
        item = parsed['probes'][0]
        assert list(item.keys()) == ['rationale', 'route']
        assert item['route'] == 'plan'
        assert 'each re-derives it.' in item['rationale']

    def test_literal_block_on_dash_line_preserves_blank_line_paragraphs(self):
        text = (
            'probes:\n'
            '  - body: |\n'
            '      para one\n'
            '\n'
            '      para two: here\n'
            '    route: plan\n'
        )
        item = parse_yaml(text)['probes'][0]
        assert list(item.keys()) == ['body', 'route']
        assert item['body'] == 'para one\n\npara two: here'

    # Regression: a top-level (unindented) block sequence of mappings — e.g.
    # `carried_items:` in coordinator/schemas/handoff.schema.json — was returned
    # as the bare list itself, discarding every scalar key already parsed
    # (including ones AFTER the sequence). parse_frontmatter then saw a list,
    # not a dict, and reported every required field as missing on a valid file.
    def test_unindented_block_sequence_of_mappings_as_last_key(self):
        text = 'title: x\ncarried_items:\n- carry_id: a\n  disposition: carried\n'
        assert parse_yaml(text) == {
            'title': 'x',
            'carried_items': [{'carry_id': 'a', 'disposition': 'carried'}],
        }

    def test_unindented_block_sequence_of_mappings_as_middle_key(self):
        text = 'title: x\ncarried_items:\n- carry_id: a\n  disposition: carried\nsummary: y\n'
        assert parse_yaml(text) == {
            'title': 'x',
            'carried_items': [{'carry_id': 'a', 'disposition': 'carried'}],
            'summary': 'y',
        }

    def test_unindented_block_sequence_of_scalars(self):
        text = 'scope:\n- a\n- b\ntitle: y\n'
        assert parse_yaml(text) == {'scope': ['a', 'b'], 'title': 'y'}

    def test_indented_block_sequence_of_scalars_still_works(self):
        text = 'scope:\n  - a\n  - b\ntitle: y\n'
        assert parse_yaml(text) == {'scope': ['a', 'b'], 'title': 'y'}

    def test_nested_mapping_with_unindented_sequence_value(self):
        text = 'outer:\n  inner_seq:\n  - a\n  - b\ntitle: y\n'
        assert parse_yaml(text) == {'outer': {'inner_seq': ['a', 'b']}, 'title': 'y'}

    def test_real_handoff_carried_items_round_trips_with_scalar_keys_intact(self):
        fixture = (
            Path(__file__).parent.parent.parent.parent
            / 'state' / 'handoffs'
            / '2026-08-06-2026-08-05_215136_2026-08-04_235721_2026-08-03_155105_claude-klabauter-oss-release.md'
        )
        fm = parse_frontmatter(fixture.read_text(encoding='utf-8'))['frontmatter']
        assert isinstance(fm, dict)
        assert fm['title']
        assert fm['status']
        assert isinstance(fm['carried_items'], list)
        assert fm['carried_items'][0]['carry_id'] == 'cf-all-families-residual-invariant-3a91c4'


class TestDescribeBehavioralCases:
    """Two behavioral cases pinned by coordinator-claude's own consult, independent of the node
    oracle: an empty optional list must be [] (not a missing key), and a schema
    with no applies_to must yield None (not a KeyError, not an absent key)."""

    def test_zero_optional_fields_is_empty_list_not_missing_key(self):
        # review-findings.schema.json v2.1.0 added the optional `reviewed_range`
        # array (2026-08-13 re-vendor); every other declared property is
        # still `required`.
        result = describe('review-findings')
        assert 'optional' in result
        assert result['optional'] == ['reviewed_range']

    def test_no_applies_to_returns_none_not_missing_key(self):
        # percolate-store.schema.json declares no applies_to glob.
        result = describe('percolate-store')
        assert 'applies_to' in result
        assert result['applies_to'] is None

    def test_unknown_schema_name_raises_value_error(self):
        with pytest.raises(ValueError, match='unknown schema'):
            describe('does-not-exist')


# =============================================================================
# validate() — T4d-g1b native replacement for `node schema-cli.js --validate`.
# =============================================================================


def _valid_bug_backlog_fields(**overrides) -> dict:
    base = {
        'created': '2026-07-05',
        'title': 'Test bug',
        'body': 'A description of the bug.',
        'status': 'open',
        'surface': 'coordinator_core',
        'severity': 'P2',
    }
    base.update(overrides)
    return base


def _valid_improvement_queue_fields(**overrides) -> dict:
    base = {
        'created': '2026-07-05',
        'title': 'Test improvement',
        'body': 'A description of the improvement.',
        'status': 'open',
        'surface': 'coordinator_core',
        'proposed_action': 'Do the thing.',
        'from_repo': 'claude-klabauter',
        'change_kind': 'doc-edit',
    }
    base.update(overrides)
    return base


class TestValidateJsonSchemaBackedDispatch:
    """validate() against a JSON-Schema-backed vendored queue schema (all 8 queue
    schemas + handoff/percolate-store are this kind)."""

    def test_valid_fields_ok(self):
        result = validate('bug-backlog', _valid_bug_backlog_fields())
        assert result == {'ok': True}

    def test_missing_required_field_rejected(self):
        fields = _valid_bug_backlog_fields()
        del fields['severity']
        result = validate('bug-backlog', fields)
        assert result['ok'] is False
        assert any(
            e['field'] == 'severity' and e['error'] == 'required field missing'
            for e in result['errors']
        )

    def test_invalid_enum_value_rejected(self):
        result = validate('bug-backlog', _valid_bug_backlog_fields(severity='P9'))
        assert result['ok'] is False
        assert any(e['field'] == 'severity' for e in result['errors'])

    def test_unknown_schema_name_raises_value_error(self):
        with pytest.raises(ValueError, match='unknown schema'):
            validate('does-not-exist', {})

    def test_valid_improvement_queue_fields_ok(self):
        result = validate('improvement-queue', _valid_improvement_queue_fields())
        assert result == {'ok': True}

    def test_improvement_queue_invalid_change_kind_rejected(self):
        result = validate(
            'improvement-queue', _valid_improvement_queue_fields(change_kind='not-a-kind')
        )
        assert result['ok'] is False
        assert any(e['field'] == 'change_kind' for e in result['errors'])


class TestValidateEnumErrorShapeParity:
    """Named per task spec: a field value VALID per JSON-Schema `type` but that FAILS
    an `enum` constraint must produce the full {field, error, hint} error dict shape,
    not merely a boolean reject — coordinator-claude's queue ops consume the shape, so shape drift
    is silent breakage."""

    def test_type_valid_enum_invalid_produces_full_error_dict_shape(self):
        # 'not-a-real-status' is a well-typed string (passes `type: string`) but is
        # not a member of status's enum — isolates the enum branch from the type branch.
        result = validate('bug-backlog', _valid_bug_backlog_fields(status='not-a-real-status'))
        assert result['ok'] is False
        status_errors = [e for e in result['errors'] if e['field'] == 'status']
        assert len(status_errors) == 1, f'expected exactly one status error, got {result["errors"]!r}'
        err = status_errors[0]
        assert set(err.keys()) == {'field', 'error', 'hint'}
        assert err['field'] == 'status'
        assert 'invalid enum value' in err['error']
        assert 'not-a-real-status' in err['error']
        assert isinstance(err['hint'], str) and err['hint'] != ''


class TestJsonSchemaNodeEnumHintParity:
    """cross-repo/archive/2026-07-23-claude-central-em-validate-frontmatter-obj-enum-hint-parity.md:
    the JSON-Schema-node enum branch (_validate_json_schema_node) omitted the trailing
    period and the _suggest_near_miss suffix its legacy-YAML twin (_validate_legacy_field,
    covered above) already emits — the two enum sites must be byte-identical in hint shape
    per schema.js:1155 / schema.js:3184 parity. Calls _validate_json_schema_node directly
    with a minimal enum-only schema, same pattern as TestSchemaShapeAllOfConditionalInert
    above."""

    def test_enum_violation_hint_has_trailing_period(self):
        schema = {'enum': ['open', 'closed']}
        errors = _validate_json_schema_node('pending', schema, schema, 'status')
        assert len(errors) == 1
        assert errors[0]['hint'] == 'Allowed values: open, closed.'

    def test_enum_near_miss_ratified_suggests_accepted(self):
        schema = {'enum': ['accepted', 'declined', 'partial']}
        errors = _validate_json_schema_node('ratified', schema, schema, 'decision')
        assert len(errors) == 1
        assert errors[0]['hint'] == "Allowed values: accepted, declined, partial. Did you mean 'accepted'?"

    def test_enum_near_miss_suppressed_when_canonical_not_in_allowed(self):
        schema = {'enum': ['open', 'closed']}
        errors = _validate_json_schema_node('ratified', schema, schema, 'status')
        assert len(errors) == 1
        assert errors[0]['hint'] == 'Allowed values: open, closed.'
        assert 'Did you mean' not in errors[0]['hint']


# =============================================================================
# allOf / oneOf / unevaluatedProperties (schema-validator-keyword-gap):
#
# cross-repo/inbox/2026-07-25-coordinator-claude-em-schema-validator-keyword-gap.md — the
# retired JS oracle walked allOf/oneOf/unevaluatedProperties generically; the Python
# port's _validate_json_schema_node did not, so schemas relying on those keywords were
# SILENTLY UNDER-VALIDATED (a malformed fixture the schema author intended to reject
# passed instead). Direct unit coverage against _validate_json_schema_node, same
# minimal-schema-dict pattern as TestJsonSchemaNodeEnumHintParity above.
# =============================================================================


class TestAllOfKeyword:
    def test_all_subschemas_pass_ok(self):
        schema = {'allOf': [{'type': 'object'}, {'required': ['a']}]}
        errors = _validate_json_schema_node({'a': 1}, schema, schema, '')
        assert errors == []

    def test_one_subschema_fails_rejected(self):
        schema = {'allOf': [{'required': ['a']}, {'required': ['b']}]}
        errors = _validate_json_schema_node({'a': 1}, schema, schema, '')
        assert len(errors) == 1
        assert errors[0]['field'] == 'b'
        assert errors[0]['error'] == 'required field missing'

    def test_multiple_failing_subschemas_collect_all_errors(self):
        # Conjunction, not short-circuit: every failing allOf branch's errors surface,
        # not just the first — a schema author needs to see everything wrong at once.
        schema = {'allOf': [{'required': ['a']}, {'required': ['b']}]}
        errors = _validate_json_schema_node({}, schema, schema, '')
        fields = {e['field'] for e in errors}
        assert fields == {'a', 'b'}

    def test_previously_silently_passing_document_now_rejected(self):
        # Mirrors the coordinator-claude memo's motivating case (strategic-self-description's
        # coordinator-root-path-omitted.json): a constraint expressed ONLY inside an
        # allOf branch, with no top-level `required`. Before this fix, allOf was not a
        # dispatched keyword at all — _validate_json_schema_node returned [] for ANY
        # schema whose only enforcement lived under allOf, however malformed the
        # document. This proves the gap is closed, not merely narrowed.
        schema = {
            'type': 'object',
            'allOf': [{'required': ['coordinator_root_path']}],
        }
        errors = _validate_json_schema_node({'other_field': 'x'}, schema, schema, '')
        assert len(errors) == 1
        assert errors[0]['field'] == 'coordinator_root_path'
        assert errors[0]['error'] == 'required field missing'


class TestOneOfKeyword:
    def test_exactly_one_match_ok(self):
        schema = {'oneOf': [{'type': 'string'}, {'type': 'number'}]}
        errors = _validate_json_schema_node('x', schema, schema, '')
        assert errors == []

    def test_zero_matches_rejected_with_distinguishing_message(self):
        schema = {'oneOf': [{'type': 'string'}, {'type': 'number'}]}
        errors = _validate_json_schema_node(True, schema, schema, '')
        assert len(errors) == 1
        assert 'does not match any allowed schema' in errors[0]['error']
        assert 'oneOf requires exactly one match' in errors[0]['error']

    def test_multiple_matches_rejected_with_distinguishing_message(self):
        # 'abc' satisfies BOTH {'type': 'string'} and {'enum': ['abc']} — overlapping
        # branches, the case a bare "oneOf failed" message cannot distinguish from
        # zero-matched.
        schema = {'oneOf': [{'type': 'string'}, {'enum': ['abc']}]}
        errors = _validate_json_schema_node('abc', schema, schema, '')
        assert len(errors) == 1
        assert 'matches 2 schemas' in errors[0]['error']
        assert 'requires exactly one match' in errors[0]['error']
        assert 'does not match any allowed schema' not in errors[0]['error']

    def test_zero_vs_multiple_error_text_is_distinct(self):
        zero_schema = {'oneOf': [{'type': 'string'}, {'type': 'number'}]}
        multi_schema = {'oneOf': [{'type': 'string'}, {'enum': ['abc']}]}
        zero_errors = _validate_json_schema_node(True, zero_schema, zero_schema, '')
        multi_errors = _validate_json_schema_node('abc', multi_schema, multi_schema, '')
        assert zero_errors[0]['error'] != multi_errors[0]['error']

    def test_sibling_keywords_still_checked_after_a_successful_match(self):
        # Fall-through discipline: a satisfied oneOf must not short-circuit the
        # node's own sibling keywords. Reinstated from the retired JS unit
        # (plugin-ecosystem/schema.test.js) whose subject `bin/lib/schema.js` was
        # deleted by the de-node port — the assertion outlives its oracle.
        schema = {
            'type': 'object',
            'oneOf': [{'required': ['a']}, {'required': ['zzz']}],
            'required': ['b'],
        }
        errors = _validate_json_schema_node({'a': 1}, schema, schema, '')
        assert [e['field'] for e in errors] == ['b']
        assert errors[0]['error'] == 'required field missing'


class TestConstKeyword:
    def test_exact_match_ok(self):
        schema = {'const': 'ready_to_fire'}
        assert _validate_json_schema_node('ready_to_fire', schema, schema, 'status') == []

    def test_mismatch_rejected(self):
        schema = {'const': 'ready_to_fire'}
        errors = _validate_json_schema_node('open', schema, schema, 'status')
        assert len(errors) == 1
        assert errors[0]['field'] == 'status'
        assert 'does not match const "ready_to_fire"' in errors[0]['error']

    def test_type_mismatched_but_string_equal_value_is_rejected(self):
        # The JS oracle needed an explicit type-mismatch note here because `==`
        # would coerce `false` and `"false"` together; Python's `!=` rejects on
        # its own. Pinned so a future rewrite toward looser comparison (or a
        # str()-based one) cannot reintroduce the coercion hole silently.
        schema = {'const': False}
        errors = _validate_json_schema_node('false', schema, schema, 'flag')
        assert len(errors) == 1
        assert errors[0]['field'] == 'flag'


class TestSchemaValuedAdditionalPropertiesKeyword:
    """Coverage for a schema-valued (non-boolean) `additionalProperties` — see
    schema_validate.py's `_validate_json_schema_node` object branch. Distinct
    from `TestUnevaluatedPropertiesKeyword`'s
    `test_fails_loud_on_schema_valued_additional_properties`, which pins that
    a schema-valued additionalProperties still fails loud when COMBINED with
    `unevaluatedProperties` — that composition is a separate, harder question
    left unsupported.
    """

    def test_conforming_undeclared_value_ok(self):
        schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
        errors = _validate_json_schema_node({'answers': 'yes'}, schema, schema, 'pm_resolution')
        assert errors == []

    def test_nested_object_in_string_slot_rejected(self):
        schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
        errors = _validate_json_schema_node(
            {'answers': {'q1': 'yes'}}, schema, schema, 'pm_resolution'
        )
        assert len(errors) == 1
        assert errors[0]['field'] == 'pm_resolution.answers'

    def test_int_value_rejected(self):
        schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
        errors = _validate_json_schema_node({'answers': 7}, schema, schema, 'pm_resolution')
        assert len(errors) == 1
        assert errors[0]['field'] == 'pm_resolution.answers'
        assert 'expected string, got int' in errors[0]['error']

    def test_list_value_rejected(self):
        schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
        errors = _validate_json_schema_node({'answers': ['a', 'b']}, schema, schema, 'pm_resolution')
        assert len(errors) == 1
        assert errors[0]['field'] == 'pm_resolution.answers'

    def test_declared_properties_still_validated_by_properties_not_additional(self):
        # A declared key is checked against `properties`, never re-checked
        # against `additionalProperties` — only undeclared keys go through
        # the additionalProperties subschema.
        schema = {
            'type': 'object',
            'properties': {'kind': {'type': 'integer'}},
            'additionalProperties': {'type': 'string'},
        }
        errors = _validate_json_schema_node({'kind': 5, 'extra': 'ok'}, schema, schema, '')
        assert errors == []

    def test_boolean_false_still_rejects_undeclared_keys_unchanged(self):
        schema = {'type': 'object', 'properties': {'a': {'type': 'string'}}, 'additionalProperties': False}
        errors = _validate_json_schema_node({'a': 'x', 'b': 'y'}, schema, schema, '')
        assert len(errors) == 1
        assert errors[0]['field'] == 'b'
        assert 'not allowed' in errors[0]['error']


class TestUnevaluatedPropertiesKeyword:
    def test_true_is_a_no_op(self):
        schema = {'type': 'object', 'unevaluatedProperties': True}
        errors = _validate_json_schema_node({'anything': 1}, schema, schema, '')
        assert errors == []

    def test_false_rejects_undeclared_key(self):
        schema = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
            'unevaluatedProperties': False,
        }
        errors = _validate_json_schema_node({'a': 'x', 'b': 'y'}, schema, schema, '')
        assert len(errors) == 1
        assert errors[0]['field'] == 'b'
        assert errors[0]['error'] == 'unevaluated property "b" not allowed'

    def test_false_allows_key_evaluated_via_allof_branch(self):
        schema = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
            'allOf': [{'properties': {'b': {'type': 'string'}}}],
            'unevaluatedProperties': False,
        }
        errors = _validate_json_schema_node({'a': 'x', 'b': 'y'}, schema, schema, '')
        assert errors == []

    def test_false_still_rejects_key_outside_allof_branch(self):
        schema = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
            'allOf': [{'properties': {'b': {'type': 'string'}}}],
            'unevaluatedProperties': False,
        }
        errors = _validate_json_schema_node({'a': 'x', 'b': 'y', 'c': 'z'}, schema, schema, '')
        assert len(errors) == 1
        assert errors[0]['field'] == 'c'

    def test_false_allows_key_evaluated_via_matched_if_then_branch(self):
        schema = {
            'type': 'object',
            'properties': {'kind': {'type': 'string'}},
            'if': {'required': ['kind']},
            'then': {'properties': {'detail': {'type': 'string'}}},
            'unevaluatedProperties': False,
        }
        errors = _validate_json_schema_node({'kind': 'x', 'detail': 'y'}, schema, schema, '')
        assert errors == []

    def test_true_additional_properties_evaluates_everything(self):
        schema = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
            'additionalProperties': True,
            'unevaluatedProperties': False,
        }
        errors = _validate_json_schema_node({'a': 'x', 'b': 'y'}, schema, schema, '')
        assert errors == []

    def test_non_boolean_value_fails_loud(self):
        schema = {'type': 'object', 'unevaluatedProperties': {'type': 'string'}}
        with pytest.raises(ValueError, match='only boolean'):
            _validate_json_schema_node({'a': 1}, schema, schema, '')

    def test_fails_loud_when_combined_with_pattern_properties(self):
        schema = {
            'type': 'object',
            'patternProperties': {'^x': {'type': 'string'}},
            'unevaluatedProperties': False,
        }
        with pytest.raises(ValueError, match='patternProperties'):
            _validate_json_schema_node({'a': 1}, schema, schema, '')

    def test_fails_loud_when_combined_with_one_of_at_same_node(self):
        schema = {
            'type': 'object',
            'oneOf': [{'required': ['a']}, {'required': ['z']}],
            'unevaluatedProperties': False,
        }
        with pytest.raises(ValueError, match='oneOf/anyOf'):
            _validate_json_schema_node({'a': 1}, schema, schema, '')

    def test_fails_loud_on_schema_valued_additional_properties(self):
        schema = {
            'type': 'object',
            'additionalProperties': {'type': 'string'},
            'unevaluatedProperties': False,
        }
        with pytest.raises(ValueError, match='schema-valued'):
            _validate_json_schema_node({'a': 1}, schema, schema, '')

    def test_fails_loud_on_nested_composition_inside_allof_branch(self):
        schema = {
            'type': 'object',
            'allOf': [{'oneOf': [{'required': ['a']}]}],
            'unevaluatedProperties': False,
        }
        with pytest.raises(ValueError, match='only one level of composition'):
            _validate_json_schema_node({'a': 1}, schema, schema, '')


# =============================================================================
# Legacy-YAML-dialect field validator (T4d-g1b) — direct unit coverage.
#
# No .yaml-dialect schema is currently vendored under
# coordinator_core/frontmatter/schemas/ (all 12 are .schema.json), so validate()'s
# legacy-dialect branch is not
# reachable through the public vendored-schema surface today. These tests
# exercise _validate_legacy_field / _validate_legacy_yaml_frontmatter directly
# against hand-built YAML-dialect schema dicts, mirroring schema.js's own
# spec shapes (schema.js:1004-1018).
# =============================================================================


class TestValidateLegacyField:
    def test_bare_string_type_string_ok(self):
        assert _validate_legacy_field('title', 'hello', 'string') == []

    def test_bare_string_type_string_wrong_type_rejected(self):
        errors = _validate_legacy_field('title', 42, 'string')
        assert len(errors) == 1
        assert errors[0] == {
            'field': 'title',
            'error': 'expected string, got number',
            'hint': 'Provide a string value for "title"',
        }

    def test_iso_date_ok(self):
        assert _validate_legacy_field('created', '2026-07-05', 'iso-date') == []

    def test_iso_date_malformed_rejected(self):
        errors = _validate_legacy_field('created', 'not-a-date', 'iso-date')
        assert len(errors) == 1
        assert errors[0]['field'] == 'created'
        assert 'expected ISO date' in errors[0]['error']

    def test_boolean_wrong_type_rejected(self):
        errors = _validate_legacy_field('pickup_ready', 'yes', 'boolean')
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected boolean, got string'

    def test_number_wrong_type_rejected(self):
        errors = _validate_legacy_field('wave', 'one', 'number')
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected number, got string'

    def test_number_bool_is_not_a_number(self):
        """JS typeof true === 'boolean', not 'number' — bool must not pass a number spec."""
        errors = _validate_legacy_field('wave', True, 'number')
        assert len(errors) == 1

    def test_enum_valid_value_ok(self):
        spec = {'type': 'enum', 'values': ['open', 'closed']}
        assert _validate_legacy_field('status', 'open', spec) == []

    def test_enum_invalid_value_rejected_with_full_shape(self):
        spec = {'type': 'enum', 'values': ['open', 'closed']}
        errors = _validate_legacy_field('status', 'pending', spec)
        assert len(errors) == 1
        assert set(errors[0].keys()) == {'field', 'error', 'hint'}
        assert errors[0]['field'] == 'status'
        assert errors[0]['error'] == 'invalid enum value "pending"'
        assert errors[0]['hint'] == 'Allowed values: open, closed.'

    def test_enum_near_miss_ratified_suggests_accepted(self):
        """Port of schema.js NEAR_MISS_CANONICAL: 'ratified' -> 'accepted' when
        'accepted' is itself a legal enum member."""
        spec = {'type': 'enum', 'values': ['accepted', 'declined', 'partial']}
        errors = _validate_legacy_field('decision', 'ratified', spec)
        assert len(errors) == 1
        assert "Did you mean 'accepted'?" in errors[0]['hint']

    def test_enum_near_miss_suppressed_when_canonical_not_in_allowed(self):
        """The near-miss suggestion only fires when the canonical replacement is
        itself a legal member of THIS field's enum — must stay silent otherwise."""
        spec = {'type': 'enum', 'values': ['open', 'closed']}
        errors = _validate_legacy_field('status', 'ratified', spec)
        assert len(errors) == 1
        assert 'Did you mean' not in errors[0]['hint']

    def test_string_or_null_accepts_null(self):
        assert _validate_legacy_field('workstream', None, {'type': 'string-or-null'}) == []

    def test_string_or_null_bare_string_form_accepts_null(self):
        assert _validate_legacy_field('workstream', None, 'string-or-null') == []

    def test_string_or_null_rejects_non_string_non_null(self):
        errors = _validate_legacy_field('workstream', 42, {'type': 'string-or-null'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected string or null, got number'

    def test_number_or_null_accepts_null(self):
        assert _validate_legacy_field('wave', None, {'type': 'number-or-null'}) == []

    def test_number_or_null_rejects_non_number_non_null(self):
        errors = _validate_legacy_field('wave', 'one', {'type': 'number-or-null'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected number or null, got string'

    def test_list_of_string_ok(self):
        assert _validate_legacy_field('tags', ['a', 'b'], {'type': 'list-of-string'}) == []

    def test_list_of_string_rejects_non_list(self):
        errors = _validate_legacy_field('tags', 'not-a-list', {'type': 'list-of-string'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected a list'

    def test_list_of_string_rejects_non_string_items(self):
        errors = _validate_legacy_field('tags', ['a', 42], {'type': 'list-of-string'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'list contains non-string items'

    def test_object_type_ok_no_subfields_spec(self):
        assert _validate_legacy_field('system', {'created_by_session': 'sess-1'}, {'type': 'object'}) == []

    def test_object_type_rejects_non_object(self):
        errors = _validate_legacy_field('system', 'not-an-object', {'type': 'object'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected object, got string'

    def test_object_type_rejects_array_as_array_not_object(self):
        errors = _validate_legacy_field('system', ['a'], {'type': 'object'})
        assert len(errors) == 1
        assert errors[0]['error'] == 'expected object, got array'

    def test_object_type_recurses_into_declared_subfields(self):
        spec = {'type': 'object', 'fields': {'tshirt': {'type': 'enum', 'values': ['S', 'M', 'L']}}}
        errors = _validate_legacy_field('loe', {'tshirt': 'XL'}, spec)
        assert len(errors) == 1
        assert errors[0]['field'] == 'loe.tshirt'

    def test_object_type_subfield_null_is_tolerated(self):
        spec = {'type': 'object', 'fields': {'tshirt': {'type': 'enum', 'values': ['S', 'M', 'L']}}}
        assert _validate_legacy_field('loe', {'tshirt': None}, spec) == []

    def test_object_type_subfield_missing_is_tolerated(self):
        spec = {'type': 'object', 'fields': {'tshirt': {'type': 'enum', 'values': ['S', 'M', 'L']}}}
        assert _validate_legacy_field('loe', {}, spec) == []

    def test_unknown_type_tag_falls_through_to_check_type_silently_passes(self):
        assert _validate_legacy_field('mystery', 'anything', {'type': 'totally-unknown'}) == []


_LEGACY_BUG_SCHEMA = {
    'schema': 'legacy-bug-fixture',
    'required': {
        'title': 'string',
        'created': 'iso-date',
        'status': {'type': 'enum', 'values': ['open', 'closed']},
    },
    'optional': {
        'severity': {'type': 'enum', 'values': ['P0', 'P1', 'P2']},
        'workstream': {'type': 'string-or-null'},
    },
}


class TestValidateLegacyYamlFrontmatter:
    def test_no_required_block_is_unconditionally_valid(self):
        assert _validate_legacy_yaml_frontmatter({'anything': 'goes'}, {}) == {'ok': True}
        assert _validate_legacy_yaml_frontmatter(None, {'applies_to': 'state/x/*.yaml'}) == {'ok': True}

    def test_missing_frontmatter_entirely_rejected(self):
        result = _validate_legacy_yaml_frontmatter(None, _LEGACY_BUG_SCHEMA)
        assert result['ok'] is False
        assert result['errors'][0]['field'] == '(frontmatter)'

    def test_valid_frontmatter_ok(self):
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'open'}
        assert _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA) == {'ok': True}

    def test_missing_required_field_rejected(self):
        fm = {'title': 'A bug', 'status': 'open'}
        result = _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA)
        assert result['ok'] is False
        assert any(e['field'] == 'created' and e['error'] == 'required field missing' for e in result['errors'])

    def test_required_field_present_but_invalid_shape_rejected(self):
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'not-a-status'}
        result = _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA)
        assert result['ok'] is False
        assert any(e['field'] == 'status' for e in result['errors'])

    def test_optional_field_absent_ok(self):
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'open'}
        assert 'severity' not in fm
        assert _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA) == {'ok': True}

    def test_optional_field_present_invalid_rejected(self):
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'open', 'severity': 'P9'}
        result = _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA)
        assert result['ok'] is False
        assert any(e['field'] == 'severity' for e in result['errors'])

    def test_optional_field_present_null_ok(self):
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'open', 'workstream': None}
        assert _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA) == {'ok': True}

    def test_no_cross_field_rules_registered_for_legacy_schema_own_name(self):
        """The schema's own 'schema' name is not in _CROSS_FIELD_RULES_BY_SCHEMA
        (that registry only has handoff/handoff-archived/cross-repo-memo) — no
        cross-field errors should ever surface for this fixture, matching
        schema.js's own CROSS_FIELD_RULES having no entry for it either."""
        fm = {'title': 'A bug', 'created': '2026-07-05', 'status': 'open'}
        result = _validate_legacy_yaml_frontmatter(fm, _LEGACY_BUG_SCHEMA)
        assert result == {'ok': True}


# ---------------------------------------------------------------------------
# Cross-field rule — gate_evidence.legs[] shape (C1,
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C1). Mirrors
# TestCarriedItemsShape's case shapes — valid/invalid leg shapes, missing
# `repo:` rejected, plus the leg_id-uniqueness check that has no schema-level
# equivalent in this repo's dependency-free JSON Schema subset.
# ---------------------------------------------------------------------------

class TestGateEvidenceLegsShape:
    def test_absent_gate_evidence_ok(self):
        errors = validate_frontmatter(_valid_handoff(), _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_human_leg_ok(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'l1', 'kind': 'human', 'reason': 'no machine-checkable predicate exists'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_file_exists_leg_ok(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'file-exists', 'repo': 'example_doctrine_repo',
                'ref': 'docs/decisions/DR-100.md', 'expected': True,
                'note': 'proves the decision record landed',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_frontmatter_field_leg_ok(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'frontmatter-field', 'repo': 'claude_klabauter',
                'ref': 'cross-repo/archive/memo.md#status', 'expected': 'actioned',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_commit_ancestor_leg_ok(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'commit-ancestor', 'repo': 'example_doctrine_repo',
                'ref': 'abc1234@refs/heads/main',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_deadline_leg_ok(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': False,
            'legs': [{'leg_id': 'l1', 'kind': 'deadline', 'ref': '2026-08-01'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_valid_test_node_id_leg_ok(self):
        """The four kinds carried verbatim from cutover.schema.json reuse the
        SAME _verified_by_ref_shape_error dispatch as _cutover_cf_verified_by_kind_and_ref_shape."""
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'test-node-id', 'repo': 'claude_klabauter',
                'ref': 'coordinator_core/tests/test_x.py::test_y',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'].startswith('gate_evidence') for e in errors)

    def test_missing_repo_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'l1', 'kind': 'commit-ancestor', 'ref': 'abc1234@refs/heads/main'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'gate_evidence.legs[0].repo' and e['error'] == 'required field missing'
            for e in errors
        )

    def test_human_leg_missing_reason_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'l1', 'kind': 'human'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].reason' for e in errors)

    def test_deadline_leg_with_repo_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': False,
            'legs': [{'leg_id': 'l1', 'kind': 'deadline', 'ref': '2026-08-01', 'repo': 'example_doctrine_repo'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(
            e['field'] == 'gate_evidence.legs[0].repo' and 'not permitted' in e['error']
            for e in errors
        )

    def test_deadline_leg_relative_ref_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': False,
            'legs': [{'leg_id': 'l1', 'kind': 'deadline', 'ref': 'in 2 weeks'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].ref' for e in errors)

    def test_file_exists_leg_missing_note_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'file-exists', 'repo': 'example_doctrine_repo',
                'ref': 'docs/decisions/DR-100.md', 'expected': True,
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].note' for e in errors)

    def test_frontmatter_field_leg_null_expected_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'frontmatter-field', 'repo': 'claude_klabauter',
                'ref': 'cross-repo/archive/memo.md#status', 'expected': None,
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].expected' for e in errors)

    def test_commit_ancestor_bare_sha_rejected(self):
        """A bare SHA with no '@' re-opens the silent-wrong-tree failure the
        mandatory repo: rule exists to prevent — ref must name both ends."""
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{'leg_id': 'l1', 'kind': 'commit-ancestor', 'repo': 'example_doctrine_repo', 'ref': 'abc1234'}],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].ref' for e in errors)

    def test_invalid_kind_enum_rejected(self):
        """Snake_case is not the ratified authoring form -- kebab is (Finding 4)."""
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [{
                'leg_id': 'l1', 'kind': 'file_exists', 'repo': 'example_doctrine_repo',
                'ref': 'docs/decisions/DR-100.md', 'expected': True, 'note': 'n',
            }],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[0].kind' and 'invalid enum value' in e['error'] for e in errors)

    def test_duplicate_leg_id_rejected(self):
        fm = _valid_handoff(gate_evidence={
            'covers_prose': True,
            'legs': [
                {'leg_id': 'l1', 'kind': 'human', 'reason': 'first'},
                {'leg_id': 'l1', 'kind': 'human', 'reason': 'second'},
            ],
        })
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs[1].leg_id' and 'duplicate' in e['error'] for e in errors)

    def test_non_list_legs_ok_shape_only(self):
        """Base shape validation (not this cross-field rule) rejects a
        non-list legs -- this rule guards against a crash on the malformed
        input, not a duplicate error."""
        fm = _valid_handoff(gate_evidence={'covers_prose': True, 'legs': 'not-a-list'})
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'gate_evidence.legs' for e in errors)


class TestLintSidecarRePlanReviewCheck:
    """`.plan-review-check.md` (the `coordinator:plan-reviewer` lens suffix,
    CONTRACT.md's plan-derivable lens table) must be an intentional member of
    `_LINT_SIDECAR_RE`'s exemption set, not an accidental substring match via
    the generic `[^./]*-review` alternative. Locks the explicit
    `plan-review-check` alternative added alongside the other three named
    plan-derivable lenses.
    """

    def test_plan_review_check_sidecar_is_exempt(self):
        assert _lint_is_sidecar_file('state/plan-sidecars/foo.plan-review-check.md') is True


class TestCfAwaitingGateNotPickupReady:
    """A ``deployment_state: awaiting_gate`` baton must not also carry
    ``pickup_ready: true`` -- a gated baton advertising pickup-readiness is a
    self-contradiction (readiness answer, not authorial-intent record).

    Spec backlink:
    cross-repo/inbox/2026-08-06-example-market-data-repo-em-pickup-ready-true-under-unmet-gate.md
    """

    def test_awaiting_gate_pickup_ready_true_rejected(self):
        fm = _valid_handoff(
            deployment_state='awaiting_gate',
            blocking_notes='waiting on upstream dependency',
            pickup_ready=True,
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert any(e['field'] == 'pickup_ready' for e in errors)

    def test_awaiting_gate_pickup_ready_absent_ok(self):
        fm = _valid_handoff(
            deployment_state='awaiting_gate',
            blocking_notes='waiting on upstream dependency',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'pickup_ready' for e in errors)

    def test_awaiting_gate_pickup_ready_false_ok(self):
        fm = _valid_handoff(
            deployment_state='awaiting_gate',
            blocking_notes='waiting on upstream dependency',
            pickup_ready=False,
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'pickup_ready' for e in errors)

    def test_ready_to_fire_pickup_ready_true_still_ok(self):
        """The three coherent arms (session-handoff, recovery, spinoff) pair
        pickup_ready: true with ready_to_fire -- this rule must not fire there."""
        fm = _valid_handoff(deployment_state='ready_to_fire', pickup_ready=True)
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert not any(e['field'] == 'pickup_ready' for e in errors)


class TestReconcileDispositionFields:
    """`reconcile_disposition` / `reconcile_disposition_reason` are read by
    coordinator_core/ops/handoff_reconcile.py's `_check_conservation` (D1
    'severed-observer' assertion) via `_DISPOSITION_FIELD` /
    `_DISPOSITION_REASON_FIELD` -- declared here as optional string
    properties on both the live and archived handoff schemas so a
    dispositioned handoff, or one carrying neither field, validates cleanly.
    """

    def test_both_fields_present_valid_on_live_handoff(self):
        fm = _valid_handoff(
            reconcile_disposition='surfaced-and-actioned',
            reconcile_disposition_reason='Reconciled manually against the successor baton.',
        )
        errors = validate_frontmatter(fm, _HANDOFF_SCHEMA)
        assert errors == []

    def test_both_fields_absent_valid_on_live_handoff(self):
        errors = validate_frontmatter(_valid_handoff(), _HANDOFF_SCHEMA)
        assert errors == []

    def test_both_fields_present_valid_on_archived_handoff(self):
        fm = _valid_archived_handoff(
            reconcile_disposition='surfaced-and-actioned',
            reconcile_disposition_reason='Reconciled manually against the successor baton.',
        )
        errors = validate_frontmatter(fm, _HANDOFF_ARCHIVED_SCHEMA)
        assert errors == []

    def test_both_fields_absent_valid_on_archived_handoff(self):
        errors = validate_frontmatter(_valid_archived_handoff(), _HANDOFF_ARCHIVED_SCHEMA)
        assert errors == []
