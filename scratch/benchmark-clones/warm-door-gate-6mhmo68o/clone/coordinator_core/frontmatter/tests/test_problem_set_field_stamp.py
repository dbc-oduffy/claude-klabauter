"""test_problem_set_field_stamp.py -- coverage for C7 of
docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md.

Purpose: DoE's census flagged `stamp-problem-set-field` and
`stamp-sizing-object-field` as "insert_fm_field plus a key" -- bind, do not
mint a new named op for either. `stamp-sizing-object-field` was already
resolved (scaffold half via `--sizing-object`, repair half via the generic
`insert_fm_field` primitive). `stamp-problem-set-field`'s only outstanding
gap was `coordinator-doc-new` emitting `# problem_set: inline` as a
COMMENTED PLACEHOLDER, never a real key -- a template line to correct, not
an adaptation to design. This suite pins the corrected scaffolder half,
mirroring test_coordinator_doc_new_sizing_object_gate.py's
ScaffoldPlanEmitsSizingObjectTest coverage for the sibling field:

1. A supplied `problem_set` value is emitted as a real frontmatter key
   (AC18's positive half).
2. Omitting `problem_set` leaves the commented-optional-key skeleton
   unchanged (no behavior change) -- unlike `sizing_object`, `problem_set`
   has no absence-declaration sentinel and no CLI-level requirement; it
   stays genuinely optional.
3. No new named op is minted -- AC18's negative half -- verified by grepping
   the module for the two candidate op names and finding none.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as
coordinator/bin/tests/test_coordinator_doc_new_sizing_object_gate.py.

Negative-spec: does NOT exercise the full CLI subprocess path (`--problem-set`
end to end) or the repair half (`insert_fm_field` against an
already-written plan) -- the repair half is the pre-existing generic
primitive, already covered by coordinator_core/frontmatter/tests/ coverage
of `insert_fm_field` itself; this suite is scoped to the scaffolder-time fix
that was C7's only positive work.

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md § C7, AC18
Spec backlink: docs/research/spike-verdicts/2026-08-21-assembler-ops-under-the-brightline.md

Run:
    pytest coordinator_core/frontmatter/tests/test_problem_set_field_stamp.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_problem_set_field_stamp_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_problem_set_field_stamp_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class ScaffoldPlanEmitsProblemSetTest(unittest.TestCase):
    """AC18: `_scaffold_plan(problem_set=...)` emits a real frontmatter key."""

    def test_supplied_problem_set_is_emitted_as_real_key(self):
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            problem_set="my-ratified-slug",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("problem_set"), "my-ratified-slug")
        self.assertNotIn("# problem_set: inline", content)

    def test_supplied_problem_set_accepts_literal_inline(self):
        content = _cli._scaffold_plan(
            title="t", branch="b", author="test-author", problem_set="inline",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("problem_set"), "inline")

    def test_omitted_problem_set_leaves_commented_skeleton_unchanged(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("problem_set", fields)
        self.assertIn(
            "# problem_set: inline               # ratified problem-set slug or 'inline'",
            content,
        )
        # The pre-existing commented-optional-key block is untouched.
        self.assertIn(
            "# Optional keys — uncomment and fill as needed (promoted de-facto keys, D1):",
            content,
        )


class NoNewNamedOpMintedTest(unittest.TestCase):
    """AC18's negative half: neither candidate op name exists in the module."""

    def test_no_stamp_problem_set_field_op(self):
        source = _CLI_PATH.read_text(encoding="utf-8")
        self.assertNotIn("stamp_problem_set_field", source)
        self.assertNotIn("stamp-problem-set-field", source)

    def test_no_stamp_sizing_object_field_op(self):
        source = _CLI_PATH.read_text(encoding="utf-8")
        self.assertNotIn("stamp_sizing_object_field", source)
        self.assertNotIn("stamp-sizing-object-field", source)


if __name__ == "__main__":
    unittest.main()
