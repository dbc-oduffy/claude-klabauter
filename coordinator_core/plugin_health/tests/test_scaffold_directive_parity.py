from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

import coordinator_core.plugin_health as plugin_health

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plugin_health_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plugin_health_scaffold_parity_test", loader
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


class HealthStatusScaffoldParityTest(unittest.TestCase):
    def _directives(self):
        result = plugin_health.brief(emit_health_status=True)
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-health-status")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-health-status")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "health-status")

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-health-status")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))

    def test_already_satisfied_key_is_boolean(self):
        directive = _directive(self._directives(), "d-scaffold-health-status")
        self.assertIsInstance(directive["already_satisfied"], bool)


class NotEmittedWithoutTriggeringInputTest(unittest.TestCase):

    def test_default_call_emits_no_directive(self):
        result = plugin_health.brief()
        self.assertEqual(result["directives"], [])

    def test_explicit_false_emits_no_directive(self):
        result = plugin_health.brief(emit_health_status=False)
        self.assertEqual(result["directives"], [])


if __name__ == "__main__":
    unittest.main()
