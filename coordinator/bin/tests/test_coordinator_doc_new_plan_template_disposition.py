"""test_coordinator_doc_new_plan_template_disposition.py -- pins the plan
scaffold's task-spine sample rows to the LIVE `disposition`/`case_against`
vocabulary, not the legacy `deferred`/`pm_approved` shape.

Purpose: `coordinator-doc-new --type plan`'s sample `## Tasks` rows are the
first thing every plan author sees. Before this change they emitted
`deferred: true` / `pm_approved: false` -- the shape docs/wiki/writing-plans.md
§ 378 marks LEGACY, read-tolerance only, no live authoring path -- so the
scaffold taught authors the retired vocabulary before they could read the
doctrine that would tell them otherwise. This suite asserts the scaffold now
emits `disposition` rows, mentions `case_against` (the strongest-honest-case
field required on `backlogged`/`wont_do`), and that the emitted sample rows
still validate against the vendored plan-tasks schema (1.10.0).

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-deferral-both-sides-adopted-three-legs-for-you.md
Spec backlink: coordinator_core/frontmatter/schemas/plan-tasks.schema.json (x-schema-version 1.10.0)

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Negative-spec: does NOT assert an interactive-prompt flow exists -- the
scaffolder is non-interactive/template-emitting (verified by reading main()),
so "prompt for case_against" is satisfied by a commented-in field at the point
of authoring, not a stdin prompt.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_template_disposition.py -v

Note: `test_sample_rows_validate_against_vendored_schema` pins the vendored
schema's `x-schema-version` by equality, not `>=`. That pin is a deliberate
tripwire, not a stray constant -- when the schema is re-vendored, the pin
mismatch forces a re-look at this scaffold's sample rows against the new
schema shape before the pin is bumped. Bump the pin when re-vendoring is
verified additive to these rows; never relax it to a range or delete the
assertion.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import unittest
from pathlib import Path

import jsonschema
import yaml

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
_SCHEMA_PATH = (
    _REPO_ROOT
    / "coordinator_core"
    / "frontmatter"
    / "schemas"
    / "plan-tasks.schema.json"
)


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_disposition_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_disposition_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()


def _scaffolded_plan_text() -> str:
    return _MOD._scaffold_plan(
        title="Sample plan for disposition template test",
        branch="main",
        author="test-author",
    )


def _extract_task_spine_rows(plan_text: str) -> list[dict]:
    match = re.search(r"```yaml plan-tasks\n(.*?)```", plan_text, re.DOTALL)
    assert match is not None, "expected exactly one `yaml plan-tasks` fenced block"
    return yaml.safe_load(match.group(1))


class TestPlanTemplateEmitsLiveDispositionVocabulary(unittest.TestCase):
    def test_no_legacy_deferred_row_shape(self):
        text = _scaffolded_plan_text()
        self.assertNotIn("deferred:", text)
        self.assertNotIn("pm_approved:", text)

    def test_emits_disposition(self):
        text = _scaffolded_plan_text()
        self.assertIn("disposition:", text)

    def test_mentions_case_against(self):
        text = _scaffolded_plan_text()
        self.assertIn("case_against", text)

    def test_case_against_is_commented_not_live_on_sample_row(self):
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        for row in rows:
            self.assertNotIn(
                "case_against",
                row,
                "sample row should not carry a live case_against -- "
                "expected an open/coded disposition needing none",
            )

    def test_sample_rows_validate_against_vendored_schema(self):
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        schema = json.loads(_SCHEMA_PATH.read_text())
        self.assertEqual(schema.get("x-schema-version"), "1.10.0")
        for row in rows:
            jsonschema.validate(instance=row, schema=schema)

    def test_sample_rows_use_open_disposition_needing_no_case_against(self):
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        for row in rows:
            self.assertEqual(row.get("disposition"), "open")


if __name__ == "__main__":
    unittest.main()
