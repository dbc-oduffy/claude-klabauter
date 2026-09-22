"""coordinator_core.roadmap_planning_assemble.tests.test_scaffold_directive_parity
-- C3's parity pin.

Purpose: `roadmap_planning_assemble` is the first host to emit through the
shared `coordinator-doc-new` directive constructor
(`scaffold_directive.build_scaffold_directive`, C1), for the two rows C0's
checked-in table (`coordinator_core/ops/doctype_hosts.py`) marks `emitted`
against this module: `roadmap-baton` and `roadmap-seed` (both keyed
`ceremony="roadmap-planning"`). This pin is the module's own instance of
the plan's § Test surface "Parity/pin per emitted type" shape
(`coordinator_core/frontmatter/tests/test_plan_scaffold_census_parity.py`'s
precedent): it calls `brief()` -- never hand-assembles a directive -- and
checks the resulting `args` against `coordinator-doc-new`'s OWN real
argument parser, so a future required-flag addition on that CLI fails this
test rather than silently authoring an invalid scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`), same idiom as
`test_plan_scaffold_census_parity.py`: `coordinator-doc-new.py` is an
extensionless-polyglot-style entry point, imported for its `_build_parser`
alone -- `main()` is never invoked, so this test writes nothing to disk and
spawns no subprocess (mirrors this module's own zero-subprocess-in-brief()
invariant, § Which discriminator this plan uses).

Covers (AC2/AC3/AC4, this module's slice):
  - `brief()`, called with entry-point-B state, emits a `roadmap-baton`
    directive whose `cli == "coordinator-doc-new"` and whose `args` parse
    clean under the real parser, with every one of the constructor's
    `required=True` flags present (`--roadmap-id`, `--stub-id`) and the
    sizing mutex resolved to exactly one of `--sizing-object`/
    `--no-sizing-object` (never both, never neither).
  - `brief()`, called with an explicit `goals` sequence, emits a
    `roadmap-seed` directive whose `args` parse clean, `--goals` present
    and comma-joined (not repeated -- `--goals` is not an `append` flag on
    the real parser), and `--out` resolves inside the repo root.
  - Every emitted directive's `--out` resolves under the repo root
    (AC4's containment, exercised end-to-end through this host rather than
    re-asserted at the constructor level -- that unit coverage is C2's).
  - Omitting both `stub_id` (entry B) and `goals` emits neither directive
    -- the additive-only, backward-compatible shape this module's
    docstring promises every pre-C3 caller.

Negative-spec: does NOT re-assert the constructor's own unit-level
behaviour (omit-when-None, `--out` escape rejection, the falsifier reds) --
that is C2's `test_scaffold_directive.py`, a sibling test module this one
does not duplicate. Does NOT assert on `already_satisfied` truthiness for
an on-disk fixture -- the constructor's own existence-stat behaviour is
C2's unit surface; this pin only asserts the key is present and boolean.
Zero subprocess: `coordinator-doc-new.py` is exec'd in-process via
`SourceFileLoader`, never invoked as a CLI. No `pytest.mark.spawns_process`.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C3.

Run:
    pytest coordinator_core/roadmap_planning_assemble/tests/test_scaffold_directive_parity.py -v
"""
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
        # `sizing_object_path` is Entry Point D's own field; D and B are
        # mutually exclusive per `_resolve_entry_point` (supplying both
        # surfaces `j-entryD-4-resolve-competing-entries` instead of
        # resolving either), so a `roadmap-baton` directive gated on
        # `entry_point == "B"` never sees a resolved sizing path today --
        # `--no-sizing-object` is the only leg this host can compute. See
        # `_roadmap_baton_and_seed_directives`'s docstring.
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
    """Additive-only: a pre-C3-shaped caller (no `stub_id`, no `goals`) gets
    neither directive -- the module docstring's backward-compatibility
    claim, pinned."""

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
