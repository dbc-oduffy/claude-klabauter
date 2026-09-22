"""coordinator_core.review_assemble.tests.test_scaffold_directive_parity --
C5's parity pin.

Purpose: `review_assemble` emits the `review-findings` scaffold through the
shared `coordinator-doc-new` directive constructor
(`scaffold_directive.build_scaffold_directive`, C1), for the one row C0's
checked-in table (`coordinator_core/ops/doctype_hosts.py`) marks `emitted`
against this module: `review-findings` (keyed `ceremony="review-assemble"`).
This pin is the module's own instance of the plan's § Test surface
"Parity/pin per emitted type" shape
(`coordinator_core/frontmatter/tests/test_plan_scaffold_census_parity.py`'s
precedent, same idiom `roadmap_planning_assemble`'s C3 pin already ships):
it calls `review_assemble.brief()` -- never hand-assembles a directive --
and checks the resulting `args` against `coordinator-doc-new`'s OWN real
argument parser, so a future required-flag addition on that CLI fails this
test rather than silently authoring an invalid scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`), same idiom as
`test_plan_scaffold_census_parity.py` and
`roadmap_planning_assemble/tests/test_scaffold_directive_parity.py`:
`coordinator-doc-new.py` is an extensionless-polyglot-style entry point,
imported for its `_build_parser` alone -- `main()` is never invoked, so
this test writes nothing to disk and spawns no subprocess (mirrors this
module's own zero-subprocess-in-`brief()` invariant, § Which discriminator
this plan uses).

Covers (AC2/AC3/AC4, this module's slice):
  - `brief(slice_id=..., scope=[...])` emits a `review-findings` directive
    whose `cli == "coordinator-doc-new"` and whose `args` parse clean under
    the real parser, with both `required=True` flags present (`--slice`,
    `--scope`) and `--scope` comma-joined (not repeated -- `--scope` is not
    an `append` flag on the real parser).
  - The emitted directive's `--out` resolves under the repo root (AC4's
    containment, exercised end-to-end through this host rather than
    re-asserted at the constructor level -- that unit coverage is C2's).
  - Omitting either `slice_id` or `scope` emits no directive at all --
    the additive-only, backward-compatible shape this module's `brief`
    docstring promises every pre-C5 caller (mirrors C3's own "neither
    emitted" pin).

Negative-spec: does NOT re-assert the constructor's own unit-level
behaviour (omit-when-None, `--out` escape rejection, the falsifier reds) --
that is C2's `roadmap_planning_assemble/tests/test_scaffold_directive.py`,
a sibling test module this one does not duplicate. Does NOT assert on
`already_satisfied` truthiness for an on-disk fixture -- the constructor's
own existence-stat behaviour is C2's unit surface; this pin only asserts
the key is present and boolean. Does NOT exercise `residue.brief` or the
`--surface` ladder -- that is `test_residue.py`'s and
`test_review_assemble_seam_exercise.py`'s surface, untouched by C5. Zero
subprocess: `coordinator-doc-new.py` is exec'd in-process via
`SourceFileLoader`, never invoked as a CLI. No `pytest.mark.spawns_process`.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C5.

Run:
    pytest coordinator_core/review_assemble/tests/test_scaffold_directive_parity.py -v
"""
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
    """Additive-only: a pre-C5-shaped caller (missing `slice_id` or
    `scope`) gets no `review-findings` directive -- the module docstring's
    backward-compatibility claim, pinned."""

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
