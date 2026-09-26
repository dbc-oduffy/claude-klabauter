from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

import coordinator_core.review_assemble as review_assemble

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_review_assemble_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_review_assemble_scaffold_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod._build_parser()


_parser = _load_doc_new_parser()


def _directive(directives: list[dict], directive_id: str) -> dict:
    for d in directives:
        if d["id"] == directive_id:
            return d
    raise AssertionError(f"no directive with id {directive_id!r} in {directives!r}")


class ReviewFindingsScaffoldParityTest(unittest.TestCase):
    def _directives(self, **kwargs):
        result = review_assemble.brief(
            slice_id="A", scope=["coordinator_core/foo.py", "coordinator_core/bar.py"], **kwargs
        )
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "review-findings")

    def test_required_flags_are_present_and_computed(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.slice_id, "A")
        self.assertEqual(args.scope, "coordinator_core/foo.py,coordinator_core/bar.py")

    def test_scope_is_comma_joined_not_repeated(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        scope_args = [a for a in directive["args"] if a.startswith("--scope=")]
        self.assertEqual(len(scope_args), 1)

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))

    def test_already_satisfied_key_is_boolean(self):
        directive = _directive(self._directives(), "d-scaffold-review-findings")
        self.assertIsInstance(directive["already_satisfied"], bool)

    def test_session_id_override_lands_in_out_path(self):
        directive = _directive(
            self._directives(session_id="sess-xyz"), "d-scaffold-review-findings"
        )
        args = _parser.parse_args(directive["args"])
        self.assertIn("sess-xyz", args.out)


class NeitherEmittedWithoutBothInputsTest(unittest.TestCase):

    def test_missing_scope_emits_no_directive(self):
        result = review_assemble.brief(slice_id="A")
        self.assertEqual(result["directives"], [])

    def test_missing_slice_id_emits_no_directive(self):
        result = review_assemble.brief(scope=["coordinator_core/foo.py"])
        self.assertEqual(result["directives"], [])

    def test_neither_supplied_emits_no_directive(self):
        result = review_assemble.brief()
        self.assertEqual(result["directives"], [])


if __name__ == "__main__":
    unittest.main()
