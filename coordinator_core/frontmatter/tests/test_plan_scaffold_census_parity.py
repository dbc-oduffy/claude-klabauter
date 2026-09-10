"""test_plan_scaffold_census_parity.py -- the plan scaffolder emits `census: []`.

Purpose: `census:` is the one mise-prep declaration a scaffolder can make
TRUTHFULLY and COMPLETELY, so it is emitted live rather than left to an author.
`coordinator/bin/mise-prep-gate.py :: _census` (DoE) treats a MISSING `census:`
as a defect and `census: []` as a pass -- `[]` is a plan asserting it rests on
no counted premise, which a reviewer can falsify by reading the plan, while a
missing census is one nobody can see. A fresh scaffold rests on no count, so
`[]` is the true value at scaffold time, and emitting it makes ADDING a census
the deliberate act instead of remembering the key.

Measured baseline this closes (DoE
`coordinator/docs/wiki/mise-prepped-authoring-bar.md` § Measured baseline):
`census:` present in 0 of 273 plans, with the remediation filed as "a scaffolded
plan needs the key added by hand" -- an operator-remembers discharge at the one
point where an artifact was available.

Two producers of one frontmatter shape, both in scope of any change to it, the
same obligation test_plan_scaffold_falsifier_parity.py and
test_plan_scaffold_brightline_parity.py assert:
  - `_scaffold_plan` in coordinator/bin/coordinator-doc-new.py, the LIVE
    producer behind `coordinator-doc-new --type plan`;
  - coordinator_core/ops/docgen/templates/plan.json, the docgen MIRROR.

Negative-spec: does NOT assert `prime_exit_criterion` becomes live. There is no
declared-empty form of a criterion -- the bar wants a non-empty sentence, so the
only live stub a scaffolder could write is a placeholder that CLEARS PRIME_EXIT
without meaning anything. That asymmetry is asserted here so a later change that
"finishes the job" by stubbing the criterion trips a test instead of a review.
Does NOT import or invoke the DoE gate: this repo's tests do not resolve into
the DoE clone, so the predicate is restated in the two terms the gate reads
(key present; value a list).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not an importable
module -- same load idiom as test_plan_scaffold_falsifier_parity.py.

Zero subprocess: the CLI module is exec'd in-process and `_scaffold_plan` is
called directly, never through the CLI. No `pytest.mark.spawns_process`.

Run:
    pytest coordinator_core/frontmatter/tests/test_plan_scaffold_census_parity.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"
_TEMPLATE_PATH = (
    _REPO_ROOT / "coordinator_core" / "ops" / "docgen" / "templates" / "plan.json"
)

_CENSUS_LINES = [
    "census: []  # counted premises as question/command/result rows; [] declares none —",
    "            # a claim a reviewer can falsify. Bar: coordinator/bin/mise-prep-gate.py.",
]


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_scaffold_census_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_scaffold_census_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _scaffold_frontmatter(**kwargs) -> dict:
    content = _cli._scaffold_plan(title="t", branch="b", author="test-author", **kwargs)
    return yaml.safe_load(content.split("---", 2)[1])


class ScaffoldEmitsDeclaredEmptyCensusTest(unittest.TestCase):
    """The gate's CENSUS predicate, restated in its own two terms."""

    def test_census_is_a_parsed_frontmatter_key(self):
        self.assertIn("census", _scaffold_frontmatter())

    def test_census_is_the_declared_empty_list_not_none(self):
        # `census:` with no value parses to None, which the gate reports as
        # "empty rather than declared-empty". The emitted form must be `[]`.
        value = _scaffold_frontmatter()["census"]
        self.assertIsInstance(value, list)
        self.assertEqual([], value)

    def test_census_survives_the_sizing_and_problem_set_arms(self):
        fields = _scaffold_frontmatter(
            sizing_object="state/sizings/example.yaml",
            problem_set="my-ratified-slug",
        )
        self.assertEqual([], fields["census"])


class PrimeExitCriterionStaysCommentedTest(unittest.TestCase):
    """The asymmetry, pinned: `census: []` is complete and true at scaffold
    time; a `prime_exit_criterion` stub would clear PRIME_EXIT while meaning
    nothing, so it stays commented. See this module's Negative-spec."""

    def test_prime_exit_criterion_is_not_a_parsed_key(self):
        self.assertNotIn("prime_exit_criterion", _scaffold_frontmatter())


class TemplateMirrorParityTest(unittest.TestCase):
    """The docgen `plan.json` mirror carries the same literal lines."""

    def _mirror_literal_lines(self) -> list[str]:
        template = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        return [
            f["line"]
            for f in template["frontmatter"]["fields"]
            if f.get("kind") == "literal"
        ]

    def test_mirror_carries_every_emitted_census_line(self):
        # Review: code-reviewer (Finding 5, minor) -- a bare per-line `assertIn` (the
        # prior form) only asks "does each line appear somewhere in the mirror," which
        # passes unchanged if the two census lines were split apart, reordered, or
        # duplicated elsewhere in plan.json. Sliced to a contiguous, positional block
        # match instead, mirroring the sibling parity tests
        # (test_scaffold_and_template_falsifier_blocks_match in
        # test_plan_scaffold_falsifier_parity.py, and the analogous brightline-parity
        # test) so a future reordering/duplication regression trips this test rather
        # than passing silently.
        mirror = self._mirror_literal_lines()
        self.assertIn(
            _CENSUS_LINES[0], mirror, f"mirror is missing emitted line: {_CENSUS_LINES[0]!r}"
        )
        start = mirror.index(_CENSUS_LINES[0])
        self.assertEqual(
            _CENSUS_LINES,
            mirror[start : start + len(_CENSUS_LINES)],
            "mirror census block is not a contiguous, positionally-matching pair",
        )

    def test_emitted_census_lines_match_the_pinned_text(self):
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        fm_lines = content.split("---", 2)[1].splitlines()
        start = fm_lines.index(_CENSUS_LINES[0])
        self.assertEqual(_CENSUS_LINES, fm_lines[start : start + len(_CENSUS_LINES)])


if __name__ == "__main__":
    unittest.main()
