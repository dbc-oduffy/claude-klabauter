"""test_coordinator_doc_new_plural_carry.py -- unit coverage for
`_scaffold_handoff`'s `deliverable_ids`/`plan_ids` plural-carrier parameters
(C1, 2026-08-19).

Purpose: a unified baton fanning in from multiple predecessors needs to
carry every parent's deliverable_id/plan_id forward, not just the single
winner the existing `--deliverable-id`/`origin_plan_id` fields carry. This
suite covers the scaffolder's own flags: `--deliverable-ids`/`--plan-ids`
(repeatable, one id per occurrence -- never comma-joined). Pure carry-
through: never resolved or minted here. Emitted as a YAML block sequence
ONLY when the flag was supplied at all -- omitted entirely (not `[]`, not
`null`) when never passed, since the schema reserves `[]` for a future
"explicitly zero" distinction. The 2+-distinct-id threshold that decides
WHEN a caller passes these flags is NOT decided here (C2 owns it) -- this
scaffolder emits exactly what it is handed.

Spec: state/dispatch-briefs/2026-08-19-unified-baton-inherits-every-parents-material/C1.md
Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § D1, D2, C3b

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_summary_gated_open.py.
Calls `_scaffold_handoff` directly, in-process, no subprocess -- avoids the
spawn-ratchet markers a CLI-shellout test would need for no added signal.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plural_carry.py -v
"""
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


# ---------------------------------------------------------------------------
# AC3/AC4 -- positive control: 2+ ids supplied are emitted as a YAML array
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# AC3/AC4 -- zero-flags emission rule: absent, never `[]`, never `null`
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Schema validation -- both the populated and absent shapes validate clean
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Kind-gate -- --deliverable-ids/--plan-ids are handoff-only, refused
# fail-loud for every other --type (same posture as --additional-predecessor/
# --summary/--gated-open/--gate-note). Exercised via main()'s argv dispatch,
# in-process (no subprocess), since the refusal lives in main()'s kind-gate
# block rather than in _scaffold_handoff itself.
# ---------------------------------------------------------------------------


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
            with self.assertRaises(SystemExit) as ctx:
                _cli.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_plan_ids_refused_for_non_handoff_type(self):
        argv = [
            "coordinator-doc-new.py",
            "--type",
            "recovery",
            "--plan-ids",
            "pln-x-abc123",
        ]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                _cli.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
