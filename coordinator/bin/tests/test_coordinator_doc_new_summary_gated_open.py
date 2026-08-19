"""test_coordinator_doc_new_summary_gated_open.py -- unit coverage for
`_scaffold_handoff`'s `summary` and `gated_open` parameters (2026-08-19).

Purpose: `session_baton.promote` needs to author the summary and the DR-173
gating trio it already has in hand at the call site, rather than every
promoted baton being born carrying the hardcoded placeholder summary and
`ready_to_fire`/`pickup_ready: true` regardless of whether the caller
actually had a summary to give it. This suite covers the scaffolder's own
two new flags: `--summary` (replaces the placeholder when supplied) and
`--gated-open` (emits the DR-173 trio -- `deployment_state: awaiting_gate`,
`pickup_ready: false`, `blocking_notes: <notes>` -- as one unit). Both are
handoff-scoped and refused fail-loud for any other `--type`, matching the
existing `--additional-predecessor` type-scoping precedent.

Spec: docs/plans/2026-08-19-promote-fills-its-own-placeholders.md, C1.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py. Calls
`_scaffold_handoff` directly for AC1/AC2/AC8 (no subprocess, no CLI argv
parsing); AC3's type-scoping guard lives in `main()`'s dispatch, so that
piece is exercised via a subprocess invocation instead.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_summary_gated_open.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from coordinator_core.frontmatter import schema_validate
from coordinator_core.win_portability import no_console_creationflags

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_summary_gated_open_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_summary_gated_open_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _frontmatter(content: str) -> dict:
    fm_text = content.split("---", 2)[1]
    return yaml.safe_load(fm_text)


# ---------------------------------------------------------------------------
# AC1 -- --summary
# ---------------------------------------------------------------------------


class ScaffoldHandoffSummaryTest(unittest.TestCase):
    def test_summary_supplied_replaces_the_placeholder(self):
        content = _cli._scaffold_handoff(title="t", branch="b", summary="did the thing")
        fields = _frontmatter(content)
        self.assertEqual(fields["summary"], "did the thing")

    def test_summary_omitted_keeps_the_placeholder_unchanged(self):
        content = _cli._scaffold_handoff(title="t", branch="b")
        fields = _frontmatter(content)
        self.assertTrue(str(fields["summary"]).startswith("PLACEHOLDER"))

    def test_blank_summary_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", summary="   ")

    def test_summary_over_140_chars_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", summary="x" * 141)

    def test_summary_at_140_chars_is_accepted(self):
        content = _cli._scaffold_handoff(title="t", branch="b", summary="x" * 140)
        fields = _frontmatter(content)
        self.assertEqual(fields["summary"], "x" * 140)


# ---------------------------------------------------------------------------
# AC2 -- --gated-open
# ---------------------------------------------------------------------------


class ScaffoldHandoffGatedOpenTest(unittest.TestCase):
    def test_gated_open_emits_the_dr173_trio(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", gated_open="summary is an unfilled placeholder"
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["deployment_state"], "awaiting_gate")
        self.assertIs(fields["pickup_ready"], False)
        self.assertEqual(fields["blocking_notes"], "summary is an unfilled placeholder")

    def test_gated_open_omitted_keeps_ready_to_fire_byte_identical(self):
        baseline = _cli._scaffold_handoff(title="t", branch="b")
        content = _cli._scaffold_handoff(title="t", branch="b", gated_open=None)
        self.assertEqual(baseline, content)
        fields = _frontmatter(content)
        self.assertEqual(fields["deployment_state"], "ready_to_fire")
        self.assertIs(fields["pickup_ready"], True)
        self.assertNotIn("blocking_notes", fields)

    def test_blank_gated_open_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", gated_open="   ")

    def test_gated_open_scaffold_validates_clean_against_handoff_schema(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", gated_open="category and summary are unfilled placeholders"
        )
        fields = _frontmatter(content)
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))


# ---------------------------------------------------------------------------
# AC8 -- absent-flag byte-identity (both flags omitted together)
# ---------------------------------------------------------------------------


class ScaffoldHandoffAbsentFlagByteIdentityTest(unittest.TestCase):
    def test_both_flags_absent_is_byte_identical_to_no_new_kwargs(self):
        with_defaults = _cli._scaffold_handoff(title="t", branch="b")
        with_explicit_none = _cli._scaffold_handoff(
            title="t", branch="b", summary=None, gated_open=None
        )
        self.assertEqual(with_defaults, with_explicit_none)


# ---------------------------------------------------------------------------
# AC3 -- both flags are handoff-scoped, refused fail-loud for any other --type
# ---------------------------------------------------------------------------


class CliTypeScopingTest(unittest.TestCase):
    def _run(self, *extra_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_CLI_PATH), *extra_args],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )

    def test_summary_rejected_for_non_handoff_type(self):
        proc = self._run("--type", "goal", "--title", "t", "--summary", "x")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--summary", proc.stderr)
        self.assertIn("--type goal", proc.stderr)

    def test_gated_open_rejected_for_non_handoff_type(self):
        proc = self._run("--type", "goal", "--title", "t", "--gated-open", "x")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--gated-open", proc.stderr)
        self.assertIn("--type goal", proc.stderr)


if __name__ == "__main__":
    unittest.main()
