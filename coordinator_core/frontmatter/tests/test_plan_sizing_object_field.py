"""Schema coverage for plan.schema.json's `sizing_object` property.

Homed in its own module rather than appended to test_schema_validate.py: that
file was under a concurrent session's claim when this coverage was authored,
and the scoped-commit gate refused a pathspec spanning both sessions' hunks.
Splitting the file is the durable fix — this coverage has no coupling to the
rest of test_schema_validate.py beyond the two helpers it re-declares below.

`sizing_object` is the FK to the sizing-object that routed a plan through
coordinator:sizing. Optional and nullable; the string arm is constrained to
state/sizings/<id>.yaml.

NEGATIVE SPEC: nothing here asserts that a cited path RESOLVES — JSON Schema
cannot check the filesystem, and the read-side existence gate deliberately
lives in coordinator_core.ops.assert_plan_sizing_citation. Nor is a citation
ever compared against a plan's problem restatement; see the plan's Anti-scope
for why intent-matching was ruled out.

Spec backlink: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C1 / AC1
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from coordinator_core.frontmatter.schema_validate import (
    parse_frontmatter,
    validate_frontmatter,
)

_SCHEMAS_DIR = Path(__file__).parent.parent / 'schemas'
_PLAN_SCHEMA = _SCHEMAS_DIR / 'plan.schema.json'


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


class TestPlanCorpusValidatesAgainstBumpedSchema:
    """No-regression differential over the ENTIRE docs/plans/*.md corpus
    (plans and review sidecars alike -- sidecar-detection heuristics are
    documented non-exhaustive and irrelevant to what this test asserts):
    every file that validated against the pre-bump schema still validates
    against the bumped one, and nothing newly fails. This is the precise form
    of AC1's "all existing plans still validate" -- a raw validate-and-count
    against the bumped schema alone conflates pre-existing, out-of-scope
    corpus defects (sidecars missing required fields, one plan with a stale
    `status: shipped` not in the schema enum) with regressions this bump could
    introduce. Comparing bumped vs. pre-bump isolates exactly the latter.
    """

    def test_no_regression_vs_pre_bump_schema(self):
        repo_root = Path(__file__).resolve().parents[3]
        plans_dir = repo_root / 'docs' / 'plans'
        plan_paths = sorted(plans_dir.glob('*.md'))
        assert len(plan_paths) > 0, f'No plans found under {plans_dir}'

        baseline_schema = json.loads(
            subprocess.run(
                ['git', 'show', 'feeb5d2c6135~1:coordinator_core/frontmatter/schemas/plan.schema.json'],
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
                if errors_after and not errors_before:
                    regressions.append(f'{path.name}: newly fails: {errors_after}')
                elif not errors_after:
                    validated_bumped += 1

            assert not regressions, (
                f'{len(regressions)} file(s) validated before the sizing_object bump '
                f'and now fail:\n' + '\n'.join(regressions)
            )
            assert validated_bumped > 0

    def test_sizing_object_bearing_plans_validate(self):
        """The plans already carrying `sizing_object:` as an unvalidated
        extension key before the bump now validate under the declared field."""
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
            'expected roughly the 17 surveyed when the field was declared.'
        )
