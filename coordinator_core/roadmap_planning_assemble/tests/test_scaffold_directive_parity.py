from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

import coordinator_core.roadmap_planning_assemble as rpa

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_roadmap_planning_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_roadmap_planning_scaffold_parity_test", loader
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


class RoadmapBatonScaffoldParityTest(unittest.TestCase):
    def _directives(self):
        result = rpa.brief(stub_id="stub-x")
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "roadmap-baton")

    def test_required_flags_are_present_and_computed(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.roadmap_id, "stub-x")
        self.assertEqual(args.stub_id, "stub-x")

    def test_sizing_mutex_resolves_to_exactly_one_leg(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        args = _parser.parse_args(directive["args"])
        present = [bool(args.sizing_object), bool(args.no_sizing_object)]
        self.assertEqual(sum(present), 1)

    def test_no_sizing_object_is_the_only_reachable_leg_at_entry_b(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        args = _parser.parse_args(directive["args"])
        self.assertTrue(args.no_sizing_object)
        self.assertIsNone(args.sizing_object)

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))

    def test_already_satisfied_key_is_boolean(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-baton")
        self.assertIsInstance(directive["already_satisfied"], bool)


class RoadmapSeedScaffoldParityTest(unittest.TestCase):
    def _directives(self, **kwargs):
        result = rpa.brief(goals=["goal-a", "goal-b"], **kwargs)
        return result["directives"]

    def test_emits_coordinator_doc_new_cli(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-seed")
        self.assertEqual(directive["cli"], "coordinator-doc-new")

    def test_args_parse_under_the_real_parser(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-seed")
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.doc_type, "roadmap-seed")

    def test_goals_is_comma_joined_not_repeated(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-seed")
        goals_args = [a for a in directive["args"] if a.startswith("--goals=")]
        self.assertEqual(len(goals_args), 1)
        args = _parser.parse_args(directive["args"])
        self.assertEqual(args.goals, "goal-a,goal-b")

    def test_out_resolves_inside_repo_root(self):
        directive = _directive(self._directives(), "d-scaffold-roadmap-seed")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        self.assertTrue(str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep))


class NeitherEmittedWithoutTriggeringInputTest(unittest.TestCase):

    def test_entry_point_a_emits_neither_scaffold(self):
        result = rpa.brief(run_id="r1", input_corpus_path="docs/corpus")
        ids = {d["id"] for d in result["directives"]}
        self.assertNotIn("d-scaffold-roadmap-baton", ids)
        self.assertNotIn("d-scaffold-roadmap-seed", ids)

    def test_entry_point_c_without_goals_emits_no_roadmap_seed(self):
        result = rpa.brief(run_id="r1", problem_set_path="docs/problems/x.md")
        ids = {d["id"] for d in result["directives"]}
        self.assertNotIn("d-scaffold-roadmap-seed", ids)


if __name__ == "__main__":
    unittest.main()
