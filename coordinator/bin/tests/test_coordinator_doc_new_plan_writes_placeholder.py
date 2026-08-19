"""test_coordinator_doc_new_plan_writes_placeholder.py -- pins the plan
scaffold's `writes:` sample VALUE and comment prose against re-teaching the
`writes: []`-as-omission-remedy inversion.

Purpose: the incident this whole slice remediates was "an author obeying the
template produced exactly the spine that could not fire" -- the scaffold's
`## Tasks` sample rows are the highest-leverage prose in the fix, since every
plan author reads them. `writes: []` is a POSITIVE claim ("writes nothing"),
not an unfilled placeholder; a row that inherits it unedited is caught only
after a wave has been dispatched and executed, versus omission, which is
caught at wave formation. This suite pins the sample value to an obviously-
unfilled concrete path instead, and pins the accompanying comment's claim
that the two empty forms (`writes: []` vs. an absent key) are NOT
interchangeable -- unguarded prose an editor could silently re-invert while
every other test in this diff stays green.

Spec backlink: coordinator_core/frontmatter/schema_validate.py
  :: _cf_plan_tasks_writes_declared (NEGATIVE SPEC section)
Spec backlink: coordinator_core/ops/dispatch_emit/pathspec.py
  (per-row zero-contribution check)

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_plan_template_disposition.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_writes_placeholder.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
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
        "coordinator_doc_new_plan_writes_placeholder_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_writes_placeholder_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()


def _scaffolded_plan_text() -> str:
    return _MOD._scaffold_plan(
        title="Sample plan for writes-placeholder template test",
        branch="main",
        author="test-author",
    )


def _extract_task_spine_rows(plan_text: str) -> list[dict]:
    match = re.search(r"```yaml plan-tasks\n(.*?)```", plan_text, re.DOTALL)
    assert match is not None, "expected exactly one `yaml plan-tasks` fenced block"
    return yaml.safe_load(match.group(1))


class TestScaffoldWritesPlaceholder(unittest.TestCase):
    def test_sample_rows_do_not_use_writes_empty_list(self):
        """`writes: []` is the semantically-loaded positive claim, not an
        unfilled placeholder -- neither sample row may use it."""
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        for row in rows:
            self.assertIn("writes", row, f"row {row.get('id')} missing writes key entirely")
            self.assertNotEqual(
                row["writes"], [],
                f"row {row.get('id')} scaffolds writes: [] -- an unedited row "
                "would claim 'writes nothing', the exact inversion this slice fixes",
            )

    def test_sample_rows_carry_an_obviously_unfilled_concrete_path(self):
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        for row in rows:
            writes = row["writes"]
            self.assertIsInstance(writes, list)
            self.assertTrue(writes, f"row {row.get('id')} writes must be non-empty placeholder")
            for path in writes:
                self.assertIn(
                    "writes.py", path,
                    f"row {row.get('id')} writes entry {path!r} does not read as an "
                    "obviously-unfilled placeholder path",
                )

    def test_sample_rows_still_validate_against_the_vendored_schema(self):
        schema = __import__("json").loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        rows = _extract_task_spine_rows(_scaffolded_plan_text())
        for row in rows:
            jsonschema.validate(instance=row, schema=schema)

    def test_writes_comment_states_the_two_empty_forms_are_not_interchangeable(self):
        """Pin the load-bearing prose itself, not just the sample value --
        this comment is the only place an unedited-row author encounters the
        distinction before hitting dispatch.emit."""
        text = _scaffolded_plan_text()
        self.assertIn("NOT interchangeable", text)
        self.assertIn("POSITIVE", text)
        self.assertIn("writes nothing", text)
        self.assertIn("UNDECLARED", text)
        self.assertIn("epistemic-premise", text)

    def test_writes_comment_does_not_instruct_empty_list_as_the_scaffold_default(self):
        """Regression pin: the comment must not read as endorsing `[]` as
        the thing to leave in place -- it must explicitly route an unknown
        surface to key-omission instead."""
        text = _scaffolded_plan_text()
        self.assertIn("omit this key entirely", text.lower())


if __name__ == "__main__":
    unittest.main()
