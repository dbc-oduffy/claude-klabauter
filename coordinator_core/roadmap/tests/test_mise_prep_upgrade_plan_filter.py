
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _load_cli_module():
    name = "mise_prep_upgrade_plan_filter_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


_PLAN = """---
title: "A plan"
created: 2026-09-08
author: test-author
status: draft
---

# Plan: a plan
"""

_SIDECAR_WITH_KIND = """---
kind: prior-art-check
plan: docs/plans/2026-09-08-subject.md
created: 2026-09-08
author: test-author
---

# Prior-art check
"""

_SIDECAR_BACKREF_ONLY = """---
plan: docs/plans/2026-09-08-subject.md
created: 2026-09-08
author: test-author
verdict: pass
---

# Coverage check
"""

_BROKEN_PLAN = """---
title: "A broken plan"
census: [
author: test-author
---

# Plan: broken frontmatter
"""


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = _cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class GlobbedNonPlanIsSkippedSilentlyTest(unittest.TestCase):

    def _corpus(self, tmp: Path) -> Path:
        plans = tmp / "plans"
        plans.mkdir()
        (plans / "2026-09-08-subject.md").write_text(_PLAN, encoding="utf-8")
        (plans / "2026-09-08-subject.prior-art-check.md").write_text(
            _SIDECAR_WITH_KIND, encoding="utf-8"
        )
        (plans / "2026-09-08-subject.plan-coverage-check.md").write_text(
            _SIDECAR_BACKREF_ONLY, encoding="utf-8"
        )
        (plans / "2026-09-08-subject.sonnet-review.md").write_text(
            "# Sonnet review\n", encoding="utf-8"
        )
        (plans / "INDEX.md").write_text("# Index\n", encoding="utf-8")
        (plans / "README.md").write_text("# Readme\n", encoding="utf-8")
        return plans

    def test_only_the_plan_is_counted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            plans = self._corpus(Path(td))
            code, out, _ = _run([str(plans), "--check", "--quiet"])
        self.assertIn("1 plan(s)", out)
        self.assertIn("0 unreadable", out)
        self.assertEqual(code, _cli.EXIT_RESIDUE)

    def test_no_non_plan_is_named_on_stdout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            plans = self._corpus(Path(td))
            _, out, err = _run([str(plans), "--check"])
        for name in (
            "prior-art-check",
            "plan-coverage-check",
            "sonnet-review",
            "INDEX.md",
            "README.md",
        ):
            self.assertNotIn(name, out, f"globbed non-plan {name} was reported")
            self.assertNotIn(name, err, f"globbed non-plan {name} was reported")
        self.assertNotIn("ERROR", out)


class BrokenPlanIsStillAnErrorTest(unittest.TestCase):

    def test_broken_plan_in_a_globbed_directory_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            plans = Path(td) / "plans"
            plans.mkdir()
            (plans / "2026-09-08-broken.md").write_text(_BROKEN_PLAN, encoding="utf-8")
            (plans / "2026-09-08-broken.prior-art-check.md").write_text(
                _SIDECAR_WITH_KIND, encoding="utf-8"
            )
            code, out, _ = _run([str(plans), "--check"])

        self.assertIn("ERROR", out)
        self.assertIn("2026-09-08-broken.md", out)
        self.assertIn("frontmatter does not parse", out)
        self.assertIn("1 plan(s)", out)
        self.assertIn("1 unreadable", out)
        self.assertEqual(code, _cli.EXIT_RESIDUE)

    def test_a_dated_plan_with_no_frontmatter_is_still_a_plan(self):
        path = Path("2026-09-08-no-frontmatter-at-all.md")
        self.assertIsNone(_cli.not_a_plan_reason(path, "# Plan\n"))


class ExplicitlyNamedFileIsNeverSilentTest(unittest.TestCase):

    def _named(self, tmp: Path, name: str, body: str):
        path = tmp / name
        path.write_text(body, encoding="utf-8")
        return _run([str(path), "--check"])

    def test_named_sidecar_gets_an_answer_and_a_usage_exit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            code, out, err = self._named(
                Path(td), "2026-09-08-subject.prior-art-check.md", _SIDECAR_WITH_KIND
            )
        self.assertEqual(code, _cli.EXIT_USAGE)
        self.assertIn("not a plan", err)
        self.assertIn("prior-art-check", err)
        self.assertNotIn("plan(s)", out)

    def test_named_index_gets_an_answer(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            code, _, err = self._named(Path(td), "INDEX.md", "# Index\n")
        self.assertEqual(code, _cli.EXIT_USAGE)
        self.assertIn("not a plan", err)

    def test_named_plan_is_still_processed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            code, out, _ = self._named(Path(td), "2026-09-08-subject.md", _PLAN)
        self.assertEqual(code, _cli.EXIT_RESIDUE)
        self.assertIn("1 plan(s)", out)


class DiscriminantIsNotKindPlanTest(unittest.TestCase):

    def test_kindless_plan_is_a_plan(self):
        self.assertIsNone(
            _cli.not_a_plan_reason(Path("2026-09-08-subject.md"), _PLAN)
        )

    def test_explicit_kind_plan_is_also_a_plan(self):
        body = _PLAN.replace("status: draft", "status: draft\nkind: plan")
        self.assertIsNone(_cli.not_a_plan_reason(Path("2026-09-08-subject.md"), body))

    def test_declared_non_plan_kind_is_not_a_plan(self):
        reason = _cli.not_a_plan_reason(
            Path("2026-09-08-subject.prior-art-check.md"), _SIDECAR_WITH_KIND
        )
        self.assertIsNotNone(reason)
        self.assertIn("prior-art-check", reason)

    def test_backref_alone_is_enough(self):
        reason = _cli.not_a_plan_reason(
            Path("2026-09-08-subject.plan-coverage-check.md"), _SIDECAR_BACKREF_ONLY
        )
        self.assertIsNotNone(reason)
        self.assertIn("plan", reason)

    def test_a_version_in_the_slug_does_not_read_as_a_sidecar_suffix(self):
        path = Path("2026-06-26-artifact-shape-contract-v1.2-example-repo-asks.md")
        self.assertIsNone(_cli.not_a_plan_reason(path, "# Plan\n"))


class NonStringRowIdTests(unittest.TestCase):
    """A spine row id is a string by convention only, and YAML believes otherwise.

    An unquoted `id: 0` parses as int. Every later use in `plan_report` assumed str, so the
    residue's `", ".join(...)` raised TypeError and took the WHOLE invocation down — a corpus
    holding one such row could not be converted at all, and the traceback named a join rather
    than the row. This is the tool a NOT-PREPPED verdict points its author at, so it has to
    survive the corpus it exists to repair. Observed on example-retrieval-repo's 2026-07-19 vector-store
    plan, whose spine numbers its rows 0-8 unquoted.
    """

    _PLAN = (
        "---\n"
        "title: numbered rows\n"
        "status: approved\n"
        "census: []\n"
        "prime_exit_criterion:\n"
        "  statement: s\n"
        "  derived_from: d\n"
        "---\n"
        "\n"
        "## Tasks\n"
        "\n"
        "```yaml plan-tasks\n"
        "- id: 0\n"
        "  title: Orient\n"
        "  change_kind: verification\n"
        "  surface: docs\n"
        "- id: 1\n"
        "  title: Build\n"
        "  change_kind: code-edit\n"
        "  surface: docs\n"
        "```\n"
    )

    def test_an_integer_row_id_does_not_crash_the_report(self):
        path = Path("docs/plans/2026-07-19-numbered.md")
        report = _cli.plan_report(path, self._PLAN)
        self.assertIsNone(report["error"])

    def test_the_integer_ids_are_named_in_the_residue(self):
        path = Path("docs/plans/2026-07-19-numbered.md")
        residue = " ".join(_cli.plan_report(path, self._PLAN)["residue"])
        self.assertIn("0", residue)
        self.assertIn("1", residue)


if __name__ == "__main__":
    unittest.main()
