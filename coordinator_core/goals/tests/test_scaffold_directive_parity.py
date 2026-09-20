"""coordinator_core.goals.tests.test_scaffold_directive_parity -- C6's
parity pin for this host.

Purpose: `goals` emits `goal` and `goal-seed` through the shared
`coordinator-doc-new` directive constructor
(`scaffold_directive.build_scaffold_directive`, C1), for the two rows
C0's checked-in table (`coordinator_core/ops/doctype_hosts.py`) marks
`emitted` against this module (both keyed `ceremony="goals"`). This pin is
the module's own instance of the plan's § Test surface "Parity/pin per
emitted type" shape (`coordinator_core/frontmatter/tests/
test_plan_scaffold_census_parity.py`'s precedent, same idiom
`roadmap_planning_assemble`'s C3 pin already ships): it calls
`goals.brief()` -- never hand-assembles a directive -- and checks the
resulting `args` against `coordinator-doc-new`'s OWN real argument parser,
so a future required-flag addition on that CLI fails this test rather
than silently authoring an invalid scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`), same idiom
as `roadmap_planning_assemble/tests/test_scaffold_directive_parity.py`:
`coordinator-doc-new.py` is an extensionless-polyglot-style entry point,
imported for its `_build_parser` alone -- `main()` is never invoked, so
this test writes nothing to disk and spawns no subprocess.

Covers (AC2/AC3/AC4, this module's slice):
  - `brief(goal_title=...)` emits a `goal` directive whose
    `cli == "coordinator-doc-new"` and whose `args` parse clean under the
    real parser, with `--title` present and `--out` resolving inside the
    repo root.
  - `brief(goal_seed_title=..., goals=[...])` emits a `goal-seed`
    directive whose `args` parse clean, `--title` present, `--goals`
    comma-joined (not repeated) when supplied, and `--out` resolving
    inside the repo root.
  - Omitting both `goal_title` and `goal_seed_title` emits neither
    directive -- the additive-only, backward-compatible shape this
    module's `brief` docstring promises every pre-C6 caller.

Negative-spec: does NOT re-assert the constructor's own unit-level
behaviour (omit-when-None, `--out` escape rejection, the falsifier reds)
-- that is C2's `roadmap_planning_assemble/tests/test_scaffold_directive.py`,
a sibling test module this one does not duplicate. Zero subprocess:
`coordinator-doc-new.py` is exec'd in-process via `SourceFileLoader`,
never invoked as a CLI. No `pytest.mark.spawns_process`.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C6.

Run:
    pytest coordinator_core/goals/tests/test_scaffold_directive_parity.py -v
"""
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
    """Additive-only: a pre-C6-shaped caller (neither `goal_title` nor
    `goal_seed_title`) gets neither directive -- the module docstring's
    backward-compatibility claim, pinned."""

    def test_neither_supplied_emits_no_directive(self):
        result = goals.brief()
        self.assertEqual(result["directives"], [])


if __name__ == "__main__":
    unittest.main()
