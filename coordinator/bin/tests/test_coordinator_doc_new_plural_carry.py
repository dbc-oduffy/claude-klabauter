from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from coordinator_core.frontmatter import schema_validate

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plural_carry_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plural_carry_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _frontmatter(content: str) -> dict:
    fm_text = content.split("---", 2)[1]
    return yaml.safe_load(fm_text)


class ScaffoldHandoffPluralCarryPositiveTest(unittest.TestCase):
    def test_deliverable_ids_supplied_emits_yaml_array(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            deliverable_ids=["dlv-alpha-abc123", "dlv-beta-def456"],
        )
        fields = _frontmatter(content)
        self.assertEqual(
            fields["deliverable_ids"], ["dlv-alpha-abc123", "dlv-beta-def456"]
        )

    def test_plan_ids_supplied_emits_yaml_array(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            plan_ids=["pln-alpha-abc123", "pln-beta-def456"],
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["plan_ids"], ["pln-alpha-abc123", "pln-beta-def456"])

    def test_both_plural_carriers_supplied_together(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            deliverable_ids=["dlv-alpha-abc123", "dlv-beta-def456"],
            plan_ids=["pln-alpha-abc123", "pln-beta-def456"],
        )
        fields = _frontmatter(content)
        self.assertEqual(
            fields["deliverable_ids"], ["dlv-alpha-abc123", "dlv-beta-def456"]
        )
        self.assertEqual(fields["plan_ids"], ["pln-alpha-abc123", "pln-beta-def456"])

    def test_plural_carriers_do_not_route_the_singular_deliverable_id(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            deliverable_id="dlv-singular-abc123",
            deliverable_ids=["dlv-alpha-abc123", "dlv-beta-def456"],
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["deliverable_id"], "dlv-singular-abc123")
        self.assertEqual(
            fields["deliverable_ids"], ["dlv-alpha-abc123", "dlv-beta-def456"]
        )


class ScaffoldHandoffPluralCarryAbsentFlagTest(unittest.TestCase):
    def test_flags_omitted_emit_no_key_at_all(self):
        content = _cli._scaffold_handoff(title="t", branch="b")
        fields = _frontmatter(content)
        self.assertNotIn("deliverable_ids", fields)
        self.assertNotIn("plan_ids", fields)
        self.assertNotIn("deliverable_ids:", content)
        self.assertNotIn("plan_ids:", content)

    def test_explicit_none_is_byte_identical_to_omitted(self):
        with_defaults = _cli._scaffold_handoff(title="t", branch="b")
        with_explicit_none = _cli._scaffold_handoff(
            title="t", branch="b", deliverable_ids=None, plan_ids=None
        )
        self.assertEqual(with_defaults, with_explicit_none)

    def test_singular_deliverable_id_and_origin_plan_id_are_untouched(self):
        baseline = _cli._scaffold_handoff(title="t", branch="b", deliverable_id="dlv-x-abc123")
        plural_omitted = _cli._scaffold_handoff(
            title="t", branch="b", deliverable_id="dlv-x-abc123", deliverable_ids=None
        )
        self.assertEqual(baseline, plural_omitted)


class ScaffoldHandoffPluralCarrySchemaTest(unittest.TestCase):
    def test_populated_plural_carriers_validate_clean_against_handoff_schema(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            deliverable_ids=["dlv-alpha-abc123", "dlv-beta-def456"],
            plan_ids=["pln-alpha-abc123", "pln-beta-def456"],
        )
        fields = _frontmatter(content)
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))

    def test_absent_plural_carriers_validate_clean_against_handoff_schema(self):
        content = _cli._scaffold_handoff(title="t", branch="b")
        fields = _frontmatter(content)
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))


class MainKindGateDeliverableIdsPlanIdsTest(unittest.TestCase):
    def test_deliverable_ids_refused_for_non_handoff_type(self):
        argv = [
            "coordinator-doc-new.py",
            "--type",
            "spinoff",
            "--deliverable-ids",
            "dlv-x-abc123",
        ]
        with mock.patch.object(sys, "argv", argv):
            try:
                code = _cli.main()
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1)

    def test_plan_ids_refused_for_non_handoff_type(self):
        argv = [
            "coordinator-doc-new.py",
            "--type",
            "recovery",
            "--plan-ids",
            "pln-x-abc123",
        ]
        with mock.patch.object(sys, "argv", argv):
            try:
                code = _cli.main()
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
