"""coordinator_core.backlog_grind_assemble.tests.test_scaffold_directive_parity
-- C6's parity pin for this host.

Purpose: `backlog_grind_assemble.directives` emits `decision` through the
shared `coordinator-doc-new` directive constructor
(`scaffold_directive.build_scaffold_directive`, C1), for the one row
C0's checked-in table (`coordinator_core/ops/doctype_hosts.py`) marks
`emitted` against this module (keyed `ceremony="backlog-grind-assemble"`).
This pin is the module's own instance of the plan's § Test surface
"Parity/pin per emitted type" shape (`coordinator_core/frontmatter/tests/
test_plan_scaffold_census_parity.py`'s precedent, same idiom
`roadmap_planning_assemble`'s C3 pin already ships): it calls
`directives.build_decision_scaffold_directive()` directly -- this
package's five reader modules are NOT in C6's footprint, so this pin
exercises the builder itself, never hand-assembling a directive -- and
checks the resulting `args` against `coordinator-doc-new`'s OWN real
argument parser, so a future required-flag addition on that CLI fails
this test rather than silently authoring an invalid scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`), same idiom
as `roadmap_planning_assemble/tests/test_scaffold_directive_parity.py`:
`coordinator-doc-new.py` is an extensionless-polyglot-style entry point,
imported for its `_build_parser` alone -- `main()` is never invoked, so
this test writes nothing to disk and spawns no subprocess.

Covers (AC2/AC3/AC4, this module's slice):
  - `build_decision_scaffold_directive(id=..., title=...)` emits a
    `decision` directive whose `cli == "coordinator-doc-new"` and whose
    `args` parse clean under the real parser, with `--title` present and
    `--out` resolving inside the repo root.
  - `dr_prefix`, when supplied, is present in `args` (`--dr-prefix`);
    omitted otherwise (the real parser's own default namespace applies).

Negative-spec: does NOT re-assert the constructor's own unit-level
behaviour (omit-when-None, `--out` escape rejection, the falsifier reds)
-- that is C2's `roadmap_planning_assemble/tests/test_scaffold_directive.py`,
a sibling test module this one does not duplicate. Does NOT exercise
`apply.py`'s dispatch loop or any of the five `readers_*.py` modules --
untouched by C6, and this builder is not yet wired into `__init__.py`'s
own `brief()` (also out of this row's footprint). Zero subprocess:
`coordinator-doc-new.py` is exec'd in-process via `SourceFileLoader`,
never invoked as a CLI. No `pytest.mark.spawns_process`.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C6.

Run:
    pytest coordinator_core/backlog_grind_assemble/tests/test_scaffold_directive_parity.py -v
"""
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
