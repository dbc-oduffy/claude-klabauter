"""test_plan_scaffold_brightline_parity.py -- `_scaffold_plan` emits the fleet
brightlines it is gated on.

Purpose: `coordinator/bin/coordinator-doc-new.py`'s `_scaffold_plan` (the LIVE
producer behind `coordinator-doc-new --type plan`) emitted no
`gated_exit_criteria` rows at all between plan.schema.json 2.10.0 -- which
introduced the field, with `minItems: 1` and one `contains` branch per fleet
brightline -- and this suite. Every plan the fleet emitted in that window was
born without the brightlines its close-out is gated on, while DoE's other
producer of the same block (`coordinator/templates/plans/plan.md.tmpl`)
scaffolded all four. Found by doe-claude-c6 on 2026-08-30, from a plan of
their own that came out of this emitter with no brightline block.

The rows are LIVE, not commented out -- the opposite call from the sibling
`prime_exit_criterion` block asserted by test_plan_scaffold_falsifier_parity.py.
That block is conditionally owed (read-side keyed on `estimate.tshirt` M/L/XL,
unknowable at scaffold time), so a live stub there would declare a criterion no
sizing asked for. These four are owed unconditionally, so a commented block
would reproduce the very defect this suite closes.

The load-bearing test here is `SlugSetMatchesVendoredSchemaTest`: it derives the
expected slug set from the vendored schema's own `brightline` enum rather than
restating it. A re-vendor that widens the enum (DoE's announced 2.13.0 adds a
fifth slug) therefore fails HERE until the emitter and the docgen mirror gain
the matching row -- the coupling between a schema bump and this producer becomes
a red test rather than something an operator has to remember.

Negative-spec: does not exercise the `coordinator-doc-new --type plan` CLI
subprocess path, and asserts nothing about `met` ever being flipped -- the
close-out gate that reads these rows is DoE-side, and no claude-klabauter consumer reads
`gated_exit_criteria` today (that absence is itself why the missing rows were
invisible here).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_plan_scaffold_falsifier_parity.py.

Run:
    pytest coordinator_core/frontmatter/tests/test_plan_scaffold_brightline_parity.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import unittest
from pathlib import Path

import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"
_TEMPLATE_PATH = (
    _REPO_ROOT / "coordinator_core" / "ops" / "docgen" / "templates" / "plan.json"
)
_SCHEMA_PATH = (
    _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas" / "plan.schema.json"
)


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_scaffold_brightline_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_scaffold_brightline_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _scaffold_frontmatter(**kwargs) -> dict:
    content = _cli._scaffold_plan(
        title="t", branch="b", author="test-author", **kwargs
    )
    return yaml.safe_load(content.split("---", 2)[1])


def _schema_brightline_enum() -> list[str]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["properties"]["gated_exit_criteria"]["items"]["properties"][
        "brightline"
    ]["enum"]


class ScaffoldEmitsLiveBrightlineRowsTest(unittest.TestCase):
    """The block parses as a real frontmatter key, not a commented skeleton."""

    def test_gated_exit_criteria_is_a_parsed_key(self):
        fields = _scaffold_frontmatter()
        self.assertIn("gated_exit_criteria", fields)
        self.assertIsInstance(fields["gated_exit_criteria"], list)

    def test_every_row_carries_the_three_required_subfields(self):
        for row in _scaffold_frontmatter()["gated_exit_criteria"]:
            self.assertEqual({"brightline", "statement", "met"}, set(row))

    def test_every_statement_is_a_non_empty_placeholder_to_replace(self):
        for row in _scaffold_frontmatter()["gated_exit_criteria"]:
            self.assertTrue(row["statement"].strip(), f"empty statement on {row}")
            self.assertIn("<REPLACE:", row["statement"])

    def test_met_starts_false_on_every_row(self):
        for row in _scaffold_frontmatter()["gated_exit_criteria"]:
            self.assertIs(row["met"], False, f"pre-set met on {row['brightline']}")

    def test_rows_survive_the_sizing_and_problem_set_arms(self):
        fields = _scaffold_frontmatter(
            sizing_object="state/sizings/example.yaml",
            problem_set="my-ratified-slug",
        )
        self.assertEqual(4, len(fields["gated_exit_criteria"]))


class SlugSetMatchesVendoredSchemaTest(unittest.TestCase):
    """The emitted slugs ARE the vendored schema's enum -- derived, not restated.

    A vendored-schema bump that widens the enum fails here until the emitter
    gains the row, which is the whole point: the schema's `contains` branches
    require every slug be present, so a widened enum with an unchanged emitter
    is a plan corpus born invalid.
    """

    def test_emitted_slugs_equal_the_schema_enum(self):
        emitted = [
            row["brightline"] for row in _scaffold_frontmatter()["gated_exit_criteria"]
        ]
        self.assertEqual(_schema_brightline_enum(), emitted)

    def test_no_slug_is_emitted_twice(self):
        emitted = [
            row["brightline"] for row in _scaffold_frontmatter()["gated_exit_criteria"]
        ]
        self.assertEqual(len(set(emitted)), len(emitted))


class ScaffoldValidatesAgainstVendoredSchemaTest(unittest.TestCase):
    """An untouched scaffold validates clean -- `contains` branches included."""

    def test_untouched_scaffold_has_no_validation_errors(self):
        errors = validate_frontmatter(_scaffold_frontmatter(), _SCHEMA_PATH)
        self.assertEqual([], errors)


class TemplateMirrorParityTest(unittest.TestCase):
    """The docgen `plan.json` mirror carries the same literal lines.

    Same obligation test_plan_scaffold_falsifier_parity.py asserts for the
    falsifier block: two producers of one frontmatter shape, both in scope of
    any change to it.
    """

    def _mirror_literal_lines(self) -> list[str]:
        template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        return [
            f["line"]
            for f in template["frontmatter"]["fields"]
            if f.get("kind") == "literal"
        ]

    def test_mirror_carries_every_emitted_brightline_line(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        fm_text = content.split("---", 2)[1]
        emitted = [
            line
            for line in fm_text.splitlines()
            if line.startswith("gated_exit_criteria:")
            or line.startswith("  - brightline:")
            or line.startswith("    statement:")
            or line.startswith("    met:")
            or line.startswith("      <REPLACE:")
            or line.startswith("      question")
            or line.startswith("      nothing works")
            or line.startswith("      assumptions were")
            or line.startswith("      a disposition")
            or line.startswith("  # ")
        ]
        self.assertTrue(emitted, "no brightline lines found in the scaffold")
        mirror = self._mirror_literal_lines()
        for line in emitted:
            self.assertIn(line, mirror, f"mirror is missing emitted line: {line!r}")


if __name__ == "__main__":
    unittest.main()
