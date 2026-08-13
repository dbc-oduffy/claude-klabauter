"""Parity tests for the handoff `kind` enum's D1 pre-rename alias tolerance
across its two independent implementations:

  - coordinator_core.frontmatter.schema_validate._tolerate_handoff_kind_aliases
    (read-side, applied to validate_frontmatter()'s output — the post-mutation
    validation the claim/pickup-assemble path routes through)
  - coordinator_core.write_guards.validate_frontmatter_schema_deny.\
    _evaluate_handoff_kind_enum (write-time hard-deny)
  - coordinator_core.frontmatter.schema_validate.\
    _tolerate_handoff_kind_aliases_in_result (the `{"ok", "errors"}`-shaped
    adapter the lint-frontmatter READ paths — `--file`'s
    _run_single_file_check and the whole-tree walk — reach the same rule
    through)

Both must agree on accept/reject for the FULL cross-product of canonical
`kind` values, every still-live `_PRE_RENAME_ALIASES` key, and a few genuine
garbage values — the named defect shape this suite guards against is "one
vocabulary, several transcriptions, no parity test" (a legacy `kind:` value
like `spinoff-roadmap` silently diverging between the two paths again).

Neither function under test touches disk beyond a schema dict this suite
constructs itself, so this suite does not require the sibling coordinator-claude
checkout (unlike write_guards/tests/test_validate_frontmatter_schema_deny.py).
"""
from __future__ import annotations

import pytest

from coordinator_core.frontmatter.baton_class import _PRE_RENAME_ALIASES
from coordinator_core.frontmatter.schema_validate import (
    _SCHEMAS_DIR,
    _run_single_file_check,
    _tolerate_handoff_kind_aliases,
    _tolerate_handoff_kind_aliases_in_result,
    _validate_json_schema_node,
    validate_frontmatter,
    validate_frontmatter_obj,
)
from coordinator_core.write_guards.validate_frontmatter_schema_deny import (
    _evaluate_handoff_kind_enum,
)

_CANONICAL_KIND_VALUES = [
    'session-handoff',
    'spinoff',
    'roadmap-baton',
    'goal-seed',
    'roadmap-seed',
    'recovery',
]

_GARBAGE_KIND_VALUES = [
    'spinoff-roadmap-typo',
    'not-a-kind',
    '',
]

_HANDOFF_KIND_SCHEMA = {
    'x-schema-name': 'handoff',
    'type': 'object',
    'properties': {
        'kind': {'enum': _CANONICAL_KIND_VALUES},
    },
}


def _schema_validate_accepts(raw_kind: str) -> bool:
    frontmatter = {'kind': raw_kind}
    shape_errors = _validate_json_schema_node(frontmatter, _HANDOFF_KIND_SCHEMA, _HANDOFF_KIND_SCHEMA, '')
    shape_errors = _tolerate_handoff_kind_aliases(
        shape_errors, 'handoff', _HANDOFF_KIND_SCHEMA, frontmatter
    )
    return not any(e['field'] == 'kind' for e in shape_errors)


def _lint_obj_path_accepts(raw_kind: str) -> bool:
    """The lint READ paths' verdict: validate_frontmatter_obj() plus the shared
    alias adapter both `--file` and the whole-tree walk now apply."""
    frontmatter = {'kind': raw_kind}
    result = _tolerate_handoff_kind_aliases_in_result(
        validate_frontmatter_obj(frontmatter, _HANDOFF_KIND_SCHEMA),
        'handoff', _HANDOFF_KIND_SCHEMA, frontmatter,
    )
    if result.get('ok'):
        return True
    return not any(e['field'] == 'kind' for e in (result.get('errors') or []))


def _deny_path_accepts(raw_kind: str) -> bool:
    message = _evaluate_handoff_kind_enum('handoff', _HANDOFF_KIND_SCHEMA, {'kind': raw_kind})
    return message is None


_ALL_KIND_VALUES = _CANONICAL_KIND_VALUES + list(_PRE_RENAME_ALIASES.keys()) + _GARBAGE_KIND_VALUES


class TestHandoffKindEnumAliasParity:
    @pytest.mark.parametrize('raw_kind', _ALL_KIND_VALUES)
    def test_schema_validate_and_deny_path_agree(self, raw_kind):
        schema_validate_verdict = _schema_validate_accepts(raw_kind)
        deny_path_verdict = _deny_path_accepts(raw_kind)
        assert schema_validate_verdict == deny_path_verdict, (
            f'kind={raw_kind!r}: schema_validate accepts={schema_validate_verdict}, '
            f'deny path accepts={deny_path_verdict} — the two handoff kind-enum '
            'checks have diverged.'
        )

    def test_canonical_values_accepted_by_both(self):
        for raw_kind in _CANONICAL_KIND_VALUES:
            assert _schema_validate_accepts(raw_kind)
            assert _deny_path_accepts(raw_kind)

    def test_pre_rename_aliases_accepted_by_both(self):
        for raw_kind in _PRE_RENAME_ALIASES:
            assert _schema_validate_accepts(raw_kind)
            assert _deny_path_accepts(raw_kind)

    def test_garbage_values_rejected_by_both(self):
        for raw_kind in _GARBAGE_KIND_VALUES:
            assert not _schema_validate_accepts(raw_kind)
            assert not _deny_path_accepts(raw_kind)


class TestLintReadPathParity:
    """`lint-frontmatter --file` (and the whole-tree walk) reach the alias rule
    through `validate_frontmatter_obj`, not `validate_frontmatter` — a second
    transcription of the vocabulary is exactly how `spinoff-roadmap` came to be
    accepted by every reader EXCEPT `--file`, rolling back baton_assemble. This
    class is the regression fence: the lint read path's verdict must equal the
    main read path's verdict for every value, tolerated and garbage alike.
    """

    @pytest.mark.parametrize('raw_kind', _ALL_KIND_VALUES)
    def test_lint_read_path_matches_main_read_path(self, raw_kind):
        main_verdict = _schema_validate_accepts(raw_kind)
        lint_verdict = _lint_obj_path_accepts(raw_kind)
        assert lint_verdict == main_verdict, (
            f'kind={raw_kind!r}: validate_frontmatter accepts={main_verdict}, '
            f'lint --file/tree-walk accepts={lint_verdict} — the two READ paths '
            'have diverged on the handoff kind vocabulary.'
        )

    def test_every_alias_the_main_path_tolerates_is_tolerated_by_lint(self):
        for raw_kind in _PRE_RENAME_ALIASES:
            assert _schema_validate_accepts(raw_kind)
            assert _lint_obj_path_accepts(raw_kind), (
                f'{raw_kind!r} is tolerated by validate_frontmatter but rejected '
                'by the lint read path'
            )

    def test_garbage_rejected_by_both_read_paths(self):
        for raw_kind in _GARBAGE_KIND_VALUES:
            assert not _schema_validate_accepts(raw_kind)
            assert not _lint_obj_path_accepts(raw_kind)

    def test_lint_adapter_leaves_non_kind_errors_untouched(self):
        """Tolerating the alias must not swallow the rest of a record's errors."""
        frontmatter = {'kind': 'spinoff-roadmap', 'title': 123}
        schema = {
            'x-schema-name': 'handoff',
            'type': 'object',
            'properties': {
                'kind': {'enum': _CANONICAL_KIND_VALUES},
                'title': {'type': 'string'},
            },
        }
        result = _tolerate_handoff_kind_aliases_in_result(
            validate_frontmatter_obj(frontmatter, schema), 'handoff', schema, frontmatter,
        )
        assert result['ok'] is False
        error_fields = [e['field'] for e in result['errors']]
        assert 'kind' not in error_fields
        assert 'title' in error_fields


def _write_handoff(repo_root, raw_kind: str):
    handoff_dir = repo_root / 'state' / 'handoffs'
    handoff_dir.mkdir(parents=True, exist_ok=True)
    path = handoff_dir / '2026-07-10-legacy-baton.md'
    path.write_text(
        '---\n'
        'title: "legacy baton"\n'
        'created: 2026-07-10\n'
        'branch: "work/test"\n'
        'status: open\n'
        'predecessor: null\n'
        f'kind: {raw_kind}\n'
        'category: roadmap\n'
        'summary: "legacy baton carrying a pre-D1 kind spelling"\n'
        'roadmap_id: "legacy-2026-07-10"\n'
        'stub_id: "legacy-01"\n'
        'wave: 1\n'
        'blocks: []\n'
        'blocked_by: []\n'
        '---\n\n'
        '# legacy baton\n',
        encoding='utf-8',
    )
    return path


class TestLintFileEndToEnd:
    """End-to-end over the real CLI entrypoint and the real vendored schema —
    the shape example-cockpit-repo's baton_assemble actually hits."""

    def test_file_mode_accepts_legacy_spinoff_roadmap(self, tmp_path, capsys):
        path = _write_handoff(tmp_path, 'spinoff-roadmap')
        exit_code = _run_single_file_check(str(tmp_path), str(path), False)
        captured = capsys.readouterr()
        assert exit_code == 0, (
            f'lint-frontmatter --file rejected a legacy spinoff-roadmap baton:\n'
            f'{captured.out}{captured.err}'
        )

    def test_file_mode_still_rejects_a_genuinely_invalid_kind(self, tmp_path, capsys):
        path = _write_handoff(tmp_path, 'definitely-not-a-kind')
        exit_code = _run_single_file_check(str(tmp_path), str(path), False)
        captured = capsys.readouterr()
        assert exit_code == 1, (
            'lint-frontmatter --file accepted a garbage kind — tolerance widened '
            f'past the alias table:\n{captured.out}{captured.err}'
        )
        assert 'kind' in captured.err


class TestSpinoffRoadmapRegression:
    """Direct regression for the reported break: a `spinoff-roadmap` legacy
    handoff must validate clean against the REAL vendored handoff schema
    (not just the synthetic schema dict the parity test above uses)."""

    def test_spinoff_roadmap_validates_clean_against_real_schema(self):
        schema_path = _SCHEMAS_DIR / 'handoff.schema.json'
        fm_dict = {
            'title': 'test handoff',
            'created': '2026-07-31',
            'branch': 'work/test',
            'status': 'active',
            'predecessor': None,
            'kind': 'spinoff-roadmap',
        }
        errors = validate_frontmatter(fm_dict, schema_path)
        kind_errors = [e for e in errors if e['field'] == 'kind']
        assert kind_errors == [], f'spinoff-roadmap should validate clean, got: {kind_errors}'
