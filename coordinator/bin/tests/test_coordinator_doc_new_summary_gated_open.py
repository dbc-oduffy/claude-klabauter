"""test_coordinator_doc_new_summary_gated_open.py -- unit coverage for
`_scaffold_handoff`'s `summary`, `gated_open`, and `gate_note` parameters
(2026-08-19).

Purpose: `session_baton.promote` needs to author the summary and the gate
state it already has in hand at the call site, rather than every promoted
baton being born carrying the hardcoded placeholder summary and
`ready_to_fire`/`pickup_ready: true` regardless of whether the caller
actually had a summary to give it. This suite covers the scaffolder's own
flags: `--summary` (replaces the placeholder when supplied), `--gated-open`
(DECLARES THE BLOCKER -- writes `blocked_by: [<id>]` and DERIVES
deployment_state/pickup_ready from it via `reconcile.gate_eval
.derive_readiness`, C1), and `--gate-note` (advisory only -- writes
`blocking_notes` and never flips readiness, 2026-08-19 ruling). All three
are handoff-scoped and refused fail-loud for any other `--type`, matching
the existing `--additional-predecessor` type-scoping precedent.

Spec: docs/plans/2026-08-19-promote-fills-its-own-placeholders.md, C1.
Spec (--gated-open re-point / --gate-note split): docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-derives-readiness.md § C3

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py. Calls
`_scaffold_handoff` directly for AC1/AC2/AC8 (no subprocess, no CLI argv
parsing); AC3's type-scoping guard lives in `main()`'s dispatch, and is
exercised by calling `_cli.main()` in-process with `sys.argv` patched
(spawn ratchet C2 disposition: STUB -- `main()` is directly importable and
callable, so a subprocess adds nothing the property under test needs;
SystemExit + a redirected `sys.stderr` stand in for `proc.returncode` /
`proc.stderr`).

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_summary_gated_open.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
import unittest.mock
from pathlib import Path

import yaml

from coordinator_core.frontmatter import schema_validate

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
# AC2 -- --gated-open declares the blocker, readiness is DERIVED (C1)
# ---------------------------------------------------------------------------


class ScaffoldHandoffGatedOpenTest(unittest.TestCase):
    def test_gated_open_declares_blocked_by_and_derives_awaiting_gate(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", gated_open="hnd-some-blocker-abc123"
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["blocked_by"], ["hnd-some-blocker-abc123"])
        self.assertEqual(fields["deployment_state"], "awaiting_gate")
        self.assertIs(fields["pickup_ready"], False)
        self.assertNotIn("blocking_notes", fields)

    def test_gated_open_omitted_keeps_ready_to_fire_byte_identical(self):
        baseline = _cli._scaffold_handoff(title="t", branch="b")
        content = _cli._scaffold_handoff(title="t", branch="b", gated_open=None)
        self.assertEqual(baseline, content)
        fields = _frontmatter(content)
        self.assertEqual(fields["deployment_state"], "ready_to_fire")
        self.assertIs(fields["pickup_ready"], True)
        self.assertNotIn("blocking_notes", fields)
        self.assertNotIn("blocked_by", fields)

    def test_blank_gated_open_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", gated_open="   ")

    def test_gated_open_scaffold_validates_clean_against_handoff_schema(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", gated_open="hnd-blocker-abc123"
        )
        fields = _frontmatter(content)
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))


# ---------------------------------------------------------------------------
# AC4 -- --gate-note is advisory only, NEVER flips readiness
# ---------------------------------------------------------------------------


class ScaffoldHandoffGateNoteTest(unittest.TestCase):
    def test_gate_note_alone_leaves_baton_pickup_ready(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", gate_note="needs a GPU box"
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["blocking_notes"], "needs a GPU box")
        self.assertEqual(fields["deployment_state"], "ready_to_fire")
        self.assertIs(fields["pickup_ready"], True)
        self.assertNotIn("blocked_by", fields)

    def test_blank_gate_note_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", gate_note="   ")

    def test_gate_note_scaffold_validates_clean_against_handoff_schema(self):
        content = _cli._scaffold_handoff(title="t", branch="b", gate_note="advisory only")
        fields = _frontmatter(content)
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))


# ---------------------------------------------------------------------------
# AC -- --gated-open and --gate-note combined is legal: a blocked baton that
# also carries a note
# ---------------------------------------------------------------------------


class ScaffoldHandoffGatedOpenAndGateNoteCombinedTest(unittest.TestCase):
    def test_both_flags_together_blocks_readiness_and_carries_the_note(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            gated_open="hnd-blocker-abc123",
            gate_note="needs a GPU box",
        )
        fields = _frontmatter(content)
        self.assertEqual(fields["blocked_by"], ["hnd-blocker-abc123"])
        self.assertEqual(fields["deployment_state"], "awaiting_gate")
        self.assertIs(fields["pickup_ready"], False)
        self.assertEqual(fields["blocking_notes"], "needs a GPU box")

    def test_both_flags_together_scaffold_validates_clean(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            gated_open="hnd-blocker-abc123",
            gate_note="needs a GPU box",
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
            title="t", branch="b", summary=None, gated_open=None, gate_note=None
        )
        self.assertEqual(with_defaults, with_explicit_none)


# ---------------------------------------------------------------------------
# AC3 -- all three flags are handoff-scoped, refused fail-loud for any other --type
# ---------------------------------------------------------------------------


class CliTypeScopingTest(unittest.TestCase):
    def _run(self, *extra_args: str) -> tuple[int, str]:
        """Run `_cli.main()` in-process with `sys.argv` patched, standing in
        for `proc.returncode`/`proc.stderr` -- see module docstring's STUB
        disposition note."""
        argv = ["coordinator-doc-new", *extra_args]
        stderr_buf = io.StringIO()
        with unittest.mock.patch("sys.argv", argv):
            with contextlib.redirect_stderr(stderr_buf):
                try:
                    raw = _cli.main()
                except SystemExit as exc:
                    raw = exc.code
                code = raw if isinstance(raw, int) else (1 if raw else 0)
                return code, stderr_buf.getvalue()

    def test_summary_rejected_for_non_handoff_type(self):
        code, stderr = self._run("--type", "goal", "--title", "t", "--summary", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("--summary", stderr)
        self.assertIn("--type goal", stderr)

    def test_gated_open_rejected_for_non_handoff_type(self):
        code, stderr = self._run("--type", "goal", "--title", "t", "--gated-open", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("--gated-open", stderr)
        self.assertIn("--type goal", stderr)

    def test_gate_note_rejected_for_non_handoff_type(self):
        code, stderr = self._run("--type", "goal", "--title", "t", "--gate-note", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("--gate-note", stderr)
        self.assertIn("--type goal", stderr)


# ---------------------------------------------------------------------------
# Asymmetry regression (C3 dispatch brief) -- the goal-seed/roadmap-seed/
# roadmap-baton `blocking_notes: PLACEHOLDER` line is a gate NOTE under the
# 2026-08-19 ruling, not a gate, and must not make a record un-pickup-ready.
# C1's derive_readiness ignores blocking_notes entirely (consult_prose_gates=
# False), so this falls out for free -- asserted here because it is the
# regression a later well-meaning edit will introduce.
# ---------------------------------------------------------------------------


class SeedPlaceholderBlockingNotesDoesNotGateTest(unittest.TestCase):
    def test_blocking_notes_placeholder_alone_does_not_flip_derived_readiness(self):
        from coordinator_core.reconcile.gate_eval import derive_readiness

        fixture = {
            "blocked_by": [],
            "blocking_notes": (
                "PLACEHOLDER — name the condition gating this baton, or delete "
                "this line once blocked_by names it"
            ),
        }
        result = derive_readiness(fixture, [])
        self.assertEqual(result["deployment_state"], "ready_to_fire")
        self.assertIs(result["pickup_ready"], True)

    def test_goal_seed_and_roadmap_seed_scaffold_the_placeholder_note(self):
        """Pins the fixture above against what the scaffolds actually emit --
        if a later edit changes the placeholder text, this fails loud rather
        than the fixture above silently drifting from reality."""
        goal_seed_content = _cli._scaffold_goal_seed(title="t", branch="b")
        roadmap_seed_content = _cli._scaffold_roadmap_seed(title="t", branch="b")
        for content in (goal_seed_content, roadmap_seed_content):
            self.assertIn("blocking_notes: PLACEHOLDER", content)


if __name__ == "__main__":
    unittest.main()
