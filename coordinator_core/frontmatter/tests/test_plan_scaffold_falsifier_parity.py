"""test_plan_scaffold_falsifier_parity.py -- coverage for C3a of
state/dispatch-briefs/2026-08-27-the-close-ceremony-refuses-a-goal-nothing-observed/C3a.md.

Purpose: `_scaffold_plan` (coordinator/bin/coordinator-doc-new.py, the LIVE
producer behind `coordinator-doc-new --type plan`) and
coordinator_core/ops/docgen/templates/plan.json (a MIRROR nothing outside
docgen/ calls) both gained a `prime_exit_criterion` falsifier skeleton --
schema 2.8.0's field names (`statement`, `derived_from`, `falsifier.how`,
`falsifier.baseline_output`, `falsifier.baseline_ref`,
`falsifier.expected_when_true`), under the `## Prime Exit Criterion` framing
carried at the frontmatter-key level (no such heading in the plan BODY --
DoE's own first attempt emitted the stub into the body where no schema
consumer reads it; see the C3a EM addendum).

AC11's decision, load-bearing: the block is COMMENTED OUT or fully empty --
never a live stub. A live stub would teach a scaffolded plan's frontmatter
to parse `prime_exit_criterion` as "declared" the moment C2's arm-2 predicate
goes live, bricking every freshly-scaffolded plan before a sizing object
exists to say whether the block is even owed (read-side rule keyed on
`estimate.tshirt` M/L/XL -- unknowable at scaffold time).

AC11a (regression): a freshly-scaffolded, untouched plan must close out
exactly as one scaffolded before C3a existed -- i.e. `prime_exit_criterion`
must not appear as a real (parsed) frontmatter key on an unedited scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_problem_set_field_stamp.py and
coordinator/bin/tests/test_coordinator_doc_new_sizing_object_gate.py.

Negative-spec: does not exercise the full CLI subprocess path or
`render_document`'s own rendering machinery for the `plan.json` mirror --
this suite is scoped to (1) the falsifier block's textual shape on both
producers and (2) the byte-for-byte parity between them, not to end-to-end
`coordinator-doc-new --type plan` invocation (already covered elsewhere,
including the pre-existing-red `test_c6_conformance.py::test_byte_identical_
to_live_oracle` for `--sizing-object` unrelated to this chunk).

Spec backlink: state/dispatch-briefs/2026-08-27-the-close-ceremony-refuses-a-goal-nothing-observed/C3a.md
Spec backlink: docs/decisions/ (AC11, AC11a, AC13 -- see C3a brief body)

Run:
    pytest coordinator_core/frontmatter/tests/test_plan_scaffold_falsifier_parity.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"
_TEMPLATE_PATH = (
    _REPO_ROOT / "coordinator_core" / "ops" / "docgen" / "templates" / "plan.json"
)

_FALSIFIER_COMMENT_LINES = [
    "#   statement:",
    "#   derived_from:",
    "#   falsifier:",
    "#     how:",
    "#     baseline_output:",
    "#     baseline_ref:",
    "#     expected_when_true:",
]


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_scaffold_falsifier_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_scaffold_falsifier_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class ScaffoldPlanEmitsCommentedFalsifierBlockTest(unittest.TestCase):
    """AC11: `_scaffold_plan` emits the falsifier skeleton fully commented out."""

    def test_falsifier_heading_is_commented(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        self.assertIn("# prime_exit_criterion:", content)
        self.assertNotIn("\nprime_exit_criterion:", content)

    def test_all_falsifier_subfields_are_commented(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        for line in _FALSIFIER_COMMENT_LINES:
            self.assertIn(
                line, content, f"expected commented falsifier line {line!r} in scaffold"
            )

    def test_expected_when_true_is_present_new_2_8_0_field(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        self.assertIn("#     expected_when_true:", content)


class ScaffoldPlanFalsifierNeverALiveStubTest(unittest.TestCase):
    """AC11a: an untouched, freshly-scaffolded plan parses identically to one
    scaffolded before C3a existed -- `prime_exit_criterion` never appears as
    a real (parsed) frontmatter key."""

    def test_prime_exit_criterion_absent_from_parsed_frontmatter(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("prime_exit_criterion", fields)

    def test_scaffold_with_sizing_and_problem_set_still_omits_the_key(self):
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            sizing_object="state/sizings/example.yaml",
            problem_set="my-ratified-slug",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("prime_exit_criterion", fields)


class TemplateMirrorParityTest(unittest.TestCase):
    """The docgen `plan.json` mirror carries the same commented falsifier
    lines as the live `_scaffold_plan` producer -- both are in this chunk's
    `writes:` and both must change, per the C3a EM addendum's correction 1."""

    def test_template_json_has_falsifier_literal_lines(self):
        template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        fm_lines = [
            f["line"]
            for f in template["frontmatter"]["fields"]
            if f.get("kind") == "literal"
        ]
        self.assertIn("# prime_exit_criterion:              # falsifier block — read-side owed only at", fm_lines)
        for line in _FALSIFIER_COMMENT_LINES:
            matches = [fl for fl in fm_lines if fl.startswith(line)]
            self.assertTrue(matches, f"expected a template literal line starting {line!r}")

    def test_template_falsifier_lines_are_all_commented(self):
        template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        fm_lines = [
            f["line"]
            for f in template["frontmatter"]["fields"]
            if f.get("kind") == "literal"
        ]
        falsifier_block = [
            fl for fl in fm_lines if "prime_exit_criterion" in fl or "falsifier" in fl
            or "statement:" in fl or "derived_from:" in fl or "baseline_" in fl
            or "expected_when_true" in fl or fl.strip().startswith("#     how:")
        ]
        self.assertTrue(falsifier_block, "no falsifier-related literal lines found in template")
        for fl in falsifier_block:
            self.assertTrue(fl.lstrip().startswith("#"), f"template line not commented: {fl!r}")

    def test_scaffold_and_template_falsifier_blocks_match(self):
        """Byte-for-byte parity between the live scaffolder's falsifier block
        and the mirror template's literal lines, modulo the scaffolder's own
        pre-existing key/value fields the template renders via non-literal
        field kinds (title/created/author/etc. -- out of scope here)."""
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        scaffold_lines = content.split("\n")
        scaffold_falsifier = [
            ln for ln in scaffold_lines if ln.strip().startswith("#") and (
                "prime_exit_criterion" in ln or "statement:" in ln
                or "derived_from:" in ln or ln.strip() == "#   falsifier:"
                or ln.strip().startswith("#     how:")
                or ln.strip().startswith("#     baseline_output:")
                or ln.strip().startswith("#     baseline_ref:")
                or ln.strip().startswith("#     expected_when_true:")
            )
        ]

        template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        fm_lines = [
            f["line"]
            for f in template["frontmatter"]["fields"]
            if f.get("kind") == "literal"
        ]
        template_falsifier = [
            fl for fl in fm_lines if fl.strip().startswith("#") and (
                "prime_exit_criterion" in fl or "statement:" in fl
                or "derived_from:" in fl or fl.strip() == "#   falsifier:"
                or fl.strip().startswith("#     how:")
                or fl.strip().startswith("#     baseline_output:")
                or fl.strip().startswith("#     baseline_ref:")
                or fl.strip().startswith("#     expected_when_true:")
            )
        ]

        self.assertEqual(scaffold_falsifier, template_falsifier)


if __name__ == "__main__":
    unittest.main()
