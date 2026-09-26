from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

from coordinator_core.backlog_grind_assemble import directives

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_backlog_grind_assemble_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_backlog_grind_assemble_scaffold_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod._build_parser()


_parser = _load_doc_new_parser()


class DecisionScaffoldParityTest(unittest.TestCase):
    def _directive(self, **kwargs):
        return directives.build_decision_scaffold_directive(
            id="d-scaffold-decision", title="Adopt the shared constructor", **kwargs
        )

    def test_emits_coordinator_doc_new_cli(self):
        directive = self._directive()
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = self._directive()
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "decision")

    def test_title_is_computed(self):
        directive = self._directive()
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.title, "Adopt the shared constructor")

    def test_dr_prefix_present_when_supplied(self):
        directive = self._directive(dr_prefix="PLATFORM")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.dr_prefix, "PLATFORM")

    def test_dr_prefix_omitted_when_not_supplied(self):
        directive = self._directive()
        dr_prefix_args = [a for a in directive["args"] if a.startswith("--dr-prefix=")]
        self.assertEqual(dr_prefix_args, [])

    def test_out_resolves_inside_repo_root(self):
        directive = self._directive()
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))

    def test_already_satisfied_key_is_boolean(self):
        directive = self._directive()
        self.assertIsInstance(directive["already_satisfied"], bool)


if __name__ == "__main__":
    unittest.main()
