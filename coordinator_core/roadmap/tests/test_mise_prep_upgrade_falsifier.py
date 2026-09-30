"""mise-prep-upgrade's two falsifier-shape derivations: nesting a top-level
`falsifier:` under `prime_exit_criterion:`, and reducing a prose
`baseline_ref` to its one unambiguous own-repo sha. Everything else declines."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

_CLI_PATH = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _load_cli_module():
    name = "mise_prep_upgrade_falsifier_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_MISNESTED = """---
title: "A plan"
census: []
created: 2026-09-11
prime_exit_criterion:
  statement: the thing holds
  derived_from: state/sizings/x.md
falsifier:
  # reviewer note kept verbatim
  how: >-
    run the probe
  baseline_output: "0"
  baseline_ref: "{ref}"
  expected_when_true: "1"
status: draft
---

# Plan
"""

_NESTED = """---
title: "A plan"
census: []
prime_exit_criterion:
  statement: the thing holds
  derived_from: state/sizings/x.md
  falsifier:
    how: run the probe
    baseline_output: "0"
    baseline_ref: "{ref}"
    expected_when_true: "1"
---

# Plan
"""


def _fm(text: str) -> dict:
    return yaml.safe_load(_cli.split_frontmatter(text)[0])


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "claude-klabauter"
        (root / ".git").mkdir(parents=True)
        (root / "docs" / "plans").mkdir(parents=True)
        self.plans = root / "docs" / "plans"

    def tearDown(self):
        self._tmp.cleanup()

    def upgrade(self, body: str):
        path = self.plans / "2026-09-11-a-plan.md"
        path.write_text(body, encoding="utf-8")
        report = _cli.plan_report(path)
        _cli.apply_derivable(path, report["derivable"])
        return report, path.read_text(encoding="utf-8")


class NestTest(_RepoCase):
    def test_misnested_block_moves_under_criterion_intact(self):
        before = _MISNESTED.format(ref="0263bc9a6")
        _, after = self.upgrade(before)
        old, new = _fm(before), _fm(after)
        self.assertNotIn("falsifier", new)
        self.assertEqual(new["prime_exit_criterion"]["falsifier"], old["falsifier"])
        self.assertIn("    # reviewer note kept verbatim", after)
        self.assertIn("status: draft\n---\n", after)
        self.assertTrue(after.endswith("# Plan\n"))

    def test_nest_and_bare_sha_compose(self):
        _, after = self.upgrade(_MISNESTED.format(ref="claude-klabauter@f36fb6e4"))
        fal = _fm(after)["prime_exit_criterion"]["falsifier"]
        self.assertEqual(fal["baseline_ref"], "f36fb6e4")
        self.assertIn("# was: claude-klabauter@f36fb6e4", after)


class BaselineRefTest(_RepoCase):
    def test_own_repo_or_unqualified_single_sha_is_reduced(self):
        for ref, sha in [
            ("HEAD 051b74401", "051b74401"),
            ("claude-klabauter@06da09b0d4", "06da09b0d4"),
            ("60569a7cd6 (HEAD at authoring, work/machine-a/2026-09-06to11)", "60569a7cd6"),
        ]:
            with self.subTest(ref=ref):
                _, after = self.upgrade(_NESTED.format(ref=ref))
                self.assertEqual(_fm(after)["prime_exit_criterion"]["falsifier"]["baseline_ref"], sha)
                self.assertIn(f"baseline_ref: {sha}  # was: {ref}\n", after)

    def test_ambiguous_refs_decline_and_stay_byte_identical(self):
        for ref in [
            "coordinator-content-repo 0263bc9a6",  # another repo's sha
            "claude-klabauter cc63e10d; coordinator-content-repo HEAD 1649952d2",
            "HEAD of work/machine-a/2026-09-06to11 at authoring time",
            "HEAD 6fcd7b7b65 (re-run of the 4338d10ab2 baseline)",
            "claude-klabauter @ bbbfb0de (first taken at 9954721197)",
            "1e076f3a2 (example-retrieval-repo-ue-addon)",
        ]:
            with self.subTest(ref=ref):
                before = _NESTED.format(ref=ref)
                report, after = self.upgrade(before)
                self.assertEqual(after, before)
                self.assertTrue(any("baseline_ref" in r for r in report["residue"]))

    def test_well_shaped_ref_is_untouched(self):
        for ref in ["0263bc9a6", "coordinator-content-repo:0263bc9a6"]:
            with self.subTest(ref=ref):
                before = _NESTED.format(ref=ref)
                report, after = self.upgrade(before)
                self.assertEqual(after, before)
                self.assertFalse(any("baseline_ref" in r for r in report["residue"]))


if __name__ == "__main__":
    unittest.main()
