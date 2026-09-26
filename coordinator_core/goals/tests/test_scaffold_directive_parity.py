from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

import coordinator_core.goals as goals

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_goals_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_goals_scaffold_parity_test", loader
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


class GoalScaffoldParityTest(unittest.TestCase):
    def _directives(self, **kwargs):
        result = goals.brief(goal_title="Ship the widget", **kwargs)
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-goal")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-goal")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "goal")

    def test_title_is_computed(self):
        directive = _directive(self._directives(), "d-scaffold-goal")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.title, "Ship the widget")

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-goal")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))

    def test_already_satisfied_key_is_boolean(self):
        directive = _directive(self._directives(), "d-scaffold-goal")
        self.assertIsInstance(directive["already_satisfied"], bool)


class GoalSeedScaffoldParityTest(unittest.TestCase):
    def _directives(self, **kwargs):
        result = goals.brief(goal_seed_title="Deferred vision slice", **kwargs)
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-goal-seed")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-goal-seed")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "goal-seed")

    def test_goals_is_comma_joined_not_repeated_when_supplied(self):
        directive = _directive(
            self._directives(goals=["goal-a", "goal-b"]), "d-scaffold-goal-seed"
        )
        goals_args = [a for a in directive["args"] if a.startswith("--goals=")]
        self.assertEqual(len(goals_args), 1)
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.goals, "goal-a,goal-b")

    def test_goals_omitted_when_not_supplied(self):
        directive = _directive(self._directives(), "d-scaffold-goal-seed")
        goals_args = [a for a in directive["args"] if a.startswith("--goals=")]
        self.assertEqual(goals_args, [])

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-goal-seed")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))


class NeitherEmittedWithoutTriggeringInputTest(unittest.TestCase):

    def test_neither_supplied_emits_no_directive(self):
        result = goals.brief()
        self.assertEqual(result["directives"], [])


if __name__ == "__main__":
    unittest.main()
