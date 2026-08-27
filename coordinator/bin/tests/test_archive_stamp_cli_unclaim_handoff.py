"""test_archive_stamp_cli_unclaim_handoff.py — argv-parsing unit test for
`archive-stamp-cli unclaim-handoff` / `unconsume-handoff`'s `--reaped-from`
scan (commit 4581f7bf6).

Two real parser defects a code review of that commit found in the
order-independent `--reaped-from` scan:

  1. A repeated `--reaped-from <sid1> --reaped-from <sid2>` silently dropped
     the second sid: `tail.index` only ever finds the FIRST occurrence, so
     after stripping it, the leftover `["--reaped-from", "sid2"]` tail set
     `note` to the literal string `"--reaped-from"` and dropped `sid2`
     entirely — no exception, no usage error. This suite asserts that shape
     is now a hard parse-time usage error (exit 2), mirroring the existing
     missing-value check.
  2. A note whose literal text is the string `"--reaped-from"` collides with
     the flag name in the shared token stream (no `--` separator convention)
     and is misparsed as the flag with a missing value. This is a
     pre-existing grammar ambiguity in the CLI's positional/flag contract,
     not fixed here — documented in the subcommand usage string instead (see
     `_SUBCOMMAND_USAGE["unclaim-handoff"]`).

Also covers the empty-string-sid normalization path (`--reaped-from ""`) and
a source-level check that the reaper's skip branches (live-children guard,
indeterminate-guard fail-closed) never invoke `_run_archive_stamp_cli` with
`--reaped-from` — only the single release-leg call site does.

The `_import_module()` seam is monkeypatched (same idiom as
test_archive_stamp_cli_ship_handoff.py) so this suite never requires
the engine root to resolve or `coordinator_core` to be importable.

Spec backlink: coordinator-code-reviewer findings 1 and 3,
state/subagent-share/d720eca0-3aba-45a9-af4b-6178f904c279/
coordinatorcode-reviewer-ef1a6f20.md, reviewed range 4581f7bf6^..4581f7bf6.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_unclaim_handoff.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator_core.ops import handoff_transition as _handoff_transition

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_unclaim_handoff_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_unclaim_handoff_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingUnclaimHandoffMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    positional args cs_unclaim_handoff was called with, so each test can
    assert the argv -> call-shape translation without a real claude-klabauter
    checkout."""

    def __init__(self):
        self.calls: list[tuple] = []

    def cs_unclaim_handoff(self, handoff_path, note, reaped_from):
        self.calls.append((handoff_path, note, reaped_from))
        return 0


class UnclaimHandoffArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingUnclaimHandoffMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_bare_path_no_note_no_flag(self):
        rc = _cli.main(["unclaim-handoff", "state/handoffs/h.md"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1], ("state/handoffs/h.md", None, None))

    def test_note_and_reaped_from_forwarded(self):
        rc = _cli.main(
            ["unclaim-handoff", "state/handoffs/h.md", "some note", "--reaped-from", "sid1"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1], ("state/handoffs/h.md", "some note", "sid1")
        )

    def test_missing_value_at_end_of_argv_is_usage_error(self):
        rc = _cli.main(["unclaim-handoff", "state/handoffs/h.md", "--reaped-from"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_unrecognized_flag_never_becomes_the_note(self):
        """Generalization of Finding 1, observed live on 2026-08-19: an EM
        typing the plausible `--note "<text>"` (the note is POSITIONAL here)
        exited 0 having written `park_note: '--note'` into a baton's
        frontmatter and silently discarded the real text. Any unrecognized
        `--flag` in the tail must be a usage error, never a note."""
        rc = _cli.main(
            ["unclaim-handoff", "state/handoffs/h.md", "--note", "the real note text"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_extra_positional_note_is_usage_error(self):
        """An unquoted multi-word note arrived as several positionals and
        every one after the first was dropped without a word — the same
        silent-corruption class, by a different route."""
        rc = _cli.main(
            ["unclaim-handoff", "state/handoffs/h.md", "first", "second"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_a_legitimate_note_with_dashes_inside_still_passes(self):
        """Only a LEADING `--` is rejected. A note containing dashes mid-text
        — which real park notes routinely do — must still forward verbatim."""
        note = "items (a), (c), (d) discharged -- (b) is the residue"
        rc = _cli.main(["unclaim-handoff", "state/handoffs/h.md", note])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1], ("state/handoffs/h.md", note, None))

    def test_duplicate_reaped_from_is_usage_error(self):
        """Regression test for Finding 1: a repeated flag must no longer
        silently drop the second sid and corrupt note into the literal
        string "--reaped-from" — it must hard-fail at parse time."""
        rc = _cli.main(
            [
                "unclaim-handoff",
                "state/handoffs/h.md",
                "--reaped-from",
                "sid1",
                "--reaped-from",
                "sid2",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_empty_string_reaped_from_is_forwarded_as_empty_string(self):
        """CLI layer forwards "" verbatim; normalization to "absent" happens
        downstream in cs_unclaim_handoff's truthy check (Finding 4) — this
        suite only asserts the argv -> call-shape translation."""
        rc = _cli.main(
            ["unclaim-handoff", "state/handoffs/h.md", "--reaped-from", ""]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1], ("state/handoffs/h.md", None, ""))

    def test_note_token_equal_to_flag_name_is_misparsed_as_flag(self):
        """Documents Finding 2's known grammar collision (not fixed here,
        documented in the usage string instead): a sole trailing token equal
        to "--reaped-from" is parsed as the flag with a missing value, not
        accepted positionally as a note."""
        rc = _cli.main(["unclaim-handoff", "state/handoffs/h.md", "--reaped-from"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_unconsume_handoff_alias_shares_the_same_scan(self):
        rc = _cli.main(
            ["unconsume-handoff", "state/handoffs/h.md", "note", "--reaped-from", "sid1"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1], ("state/handoffs/h.md", "note", "sid1"))


class ReaperSkipBranchesNeverPassReapedFromTest(unittest.TestCase):
    """The reaper's release leg carries `reaped_from` provenance and no other
    leg does (`docs/plans/2026-08-05-reaper-preserves-closure-evidence.md`).

    RE-POINTED 2026-08-26 (DR-362). This was a static regex scan of
    `coordinator/bin/reap-orphaned-in-flight-handoffs.py` for
    `_run_archive_stamp_cli(...)` call sites carrying `--reaped-from`, because
    at the time "a subprocess-level assertion would require standing up a full
    reaper fixture". That CLI was deleted at 515.6ms under DR-344 section 6 and
    the write path now lives in
    `coordinator_core.ops.reap_in_flight_claims.apply_dispositions`, which takes
    a plain list of dispositions and calls `archive_stamp`'s verbs in-process.
    So the fixture excuse is gone and this is now a BEHAVIOURAL assertion over
    that function, not a grep over source text.

    The property is unchanged and is the point: releasing a crash-orphaned claim
    must record who it was reaped from, and no skip verdict may write at all."""

    def _run(self, dispositions):
        from coordinator_core.ops import reap_in_flight_claims as reaper

        calls = []
        real_unclaim = reaper.cs_unclaim_handoff
        real_ship = reaper.cs_ship_handoff

        reaper.cs_unclaim_handoff = lambda path, reaped_from=None: (
            calls.append(("unclaim", path, reaped_from)), 0)[1]
        reaper.cs_ship_handoff = lambda path, sha=None: (
            calls.append(("ship", path, sha)), 0)[1]
        try:
            reaper.apply_dispositions(dispositions)
        finally:
            reaper.cs_unclaim_handoff = real_unclaim
            reaper.cs_ship_handoff = real_ship
        return calls

    def test_release_leg_carries_reaped_from(self):
        from coordinator_core.ops import reap_in_flight_claims as reaper

        calls = self._run([
            reaper.Disposition("state/handoffs/a.md", "dead1", reaper._VERDICT_RELEASE, "d"),
        ])
        self.assertEqual(calls, [("unclaim", "state/handoffs/a.md", "dead1")])

    def test_no_other_leg_carries_reaped_from(self):
        from coordinator_core.ops import reap_in_flight_claims as reaper

        calls = self._run([
            reaper.Disposition("state/handoffs/b.md", "dead2",
                               reaper._VERDICT_RECLAIM_SHIPPED, "d", sha="abc123"),
        ])
        self.assertNotIn("unclaim", [c[0] for c in calls])
        # `cs_ship_handoff` ALONE — `apply_dispositions`'s negative-spec forbids a
        # standalone `stamp_shipped_in` ahead of it, because on a guard-retained
        # handoff the pre-stamp survives a flip that never happened and leaves the
        # shipped_in/in_flight half-state the composed verb exists to close.
        self.assertEqual([c[0] for c in calls], ["ship"])

    def test_skip_verdicts_write_nothing_at_all(self):
        from coordinator_core.ops import reap_in_flight_claims as reaper

        calls = self._run([
            reaper.Disposition("state/handoffs/c.md", "dead3",
                               reaper._VERDICT_SKIP_LIVE_CHILDREN, "d"),
            reaper.Disposition("state/handoffs/d.md", "dead4",
                               reaper._VERDICT_SKIP_GOVERNED_PLAN, "d"),
        ])
        self.assertEqual(calls, [])


class UnclaimSessionLedgerAdvisoryTest(unittest.TestCase):
    """Direct-call coverage for `_unclaim`'s Session Ledger discharge-evidence
    advisory (docs/plans/2026-08-14-discharge-evidence-at-the-unclaim-seam.md,
    C1). Unlike the argv-parsing suite above, this calls
    `coordinator_core.ops.handoff_transition._unclaim` directly against a real
    on-disk handoff fixture — the behaviour under test (frontmatter mutation +
    body parsing) lives below the CLI/op-handler seam those tests cover.

    `locked_rmw` is monkeypatched to a minimal in-process read/mutate/write —
    this is a unit test of `_unclaim`'s `mutate()` logic, not an integration
    test of the cross-process flock (which needs a real git common dir this
    suite has no reason to stand up).

    Both sid abbreviation directions are exercised (AC2): the corpus and the
    canonical writer (`format_oneline_row`) disagree on which end of the full
    session id a ledger row's 6-char column abbreviates — see
    `_unclaim_session_ledger_notice`'s docstring."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.worktree = Path(self._tmpdir.name)
        self.repo_root = self.worktree  # never dereferenced by the fake below

        def _fake_locked_rmw(target, mutate, *, repo_root, timeout=None, missing_ok=False):
            old_text = target.read_text(encoding="utf-8")
            new_text = mutate(old_text)
            if new_text != old_text:
                target.write_text(new_text, encoding="utf-8")
            return new_text

        patcher = mock.patch.object(_handoff_transition, "locked_rmw", _fake_locked_rmw)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_handoff(self, *, claimed_by: str, ledger_lines=None) -> Path:
        handoffs_dir = self.worktree / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        body_lines = ["", "## What this covers", "", "Test fixture body.", ""]
        if ledger_lines is not None:
            body_lines += ["## Session Ledger", "", *ledger_lines, ""]
        content = (
            "---\n"
            'title: "Test handoff for unclaim ledger advisory"\n'
            "created: 2026-08-14\n"
            'branch: "work/test/branch"\n'
            "status: claimed\n"
            "predecessor: none\n"
            "deployment_state: in_flight\n"
            "claimed_at: '2026-08-14T00:00:00Z'\n"
            f"claimed_by: {claimed_by}\n"
            "category: infra\n"
            "summary: 'Test fixture for the unclaim ledger advisory suite'\n"
            "---\n" + "\n".join(body_lines)
        )
        path = handoffs_dir / "test-handoff.md"
        path.write_text(content, encoding="utf-8")
        return path

    # Real corpus example (state/handoffs/2026-08-14-retire-coordinator-venv.md):
    # ledger row "c973de" against full id c973dea9-...-b5538c7cb3b6 — a LEADING-6
    # match, the convention the live corpus actually uses.
    _FULL_SID = "c973dea9-fab3-462d-8c3b-b5538c7cb3b6"
    _OTHER_CLAIMER = "931ad709-a702-4279-98b6-0b47ec3af140"
    _EXPECTED_NOTICE = (
        "ledger row for this session present — unclaimed anyway; "
        "ship-handoff marks it finished instead"
    )

    def test_leading_six_hit_warns_and_still_applies(self):
        path = self._write_handoff(
            claimed_by=self._OTHER_CLAIMER,
            ledger_lines=["2026-08-13 | c973de | S | 3d / 1o | did some work"],
        )
        result = _handoff_transition._unclaim(
            str(path), "", self.worktree, self.repo_root, session_id=self._FULL_SID
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["applied"])
        self.assertIn(self._EXPECTED_NOTICE, result["message"])
        new_text = path.read_text(encoding="utf-8")
        self.assertIn("status: open", new_text)
        self.assertIn("deployment_state: ready_to_fire", new_text)

    def test_trailing_six_hit_also_warns(self):
        path = self._write_handoff(
            claimed_by=self._OTHER_CLAIMER,
            ledger_lines=["2026-08-13 | 7cb3b6 | S | 3d / 1o | did some work"],
        )
        result = _handoff_transition._unclaim(
            str(path), "", self.worktree, self.repo_root, session_id=self._FULL_SID
        )
        self.assertTrue(result["applied"])
        self.assertIn(self._EXPECTED_NOTICE, result["message"])

    def test_different_session_row_does_not_warn(self):
        path = self._write_handoff(
            claimed_by=self._OTHER_CLAIMER,
            ledger_lines=["2026-08-13 | ffffff | S | 3d / 1o | someone else's work"],
        )
        result = _handoff_transition._unclaim(
            str(path), "", self.worktree, self.repo_root, session_id=self._FULL_SID
        )
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["message"],
            f"unclaimed {str(path)} (status: open, deployment_state: ready_to_fire)",
        )

    def test_no_ledger_block_behaves_as_today(self):
        path = self._write_handoff(claimed_by=self._OTHER_CLAIMER, ledger_lines=None)
        result = _handoff_transition._unclaim(
            str(path), "", self.worktree, self.repo_root, session_id=self._FULL_SID
        )
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["message"],
            f"unclaimed {str(path)} (status: open, deployment_state: ready_to_fire)",
        )

    def test_no_session_id_never_blocks_unclaim(self):
        """AC2c: an unresolvable/absent session_id is silently no-warn — the
        advisory must never fail loud onto the release path it rides on
        (unlike _claim's fail-loud empty-session_id gate)."""
        path = self._write_handoff(
            claimed_by=self._OTHER_CLAIMER,
            ledger_lines=["2026-08-13 | c973de | S | 3d / 1o | did some work"],
        )
        result = _handoff_transition._unclaim(
            str(path), "", self.worktree, self.repo_root, session_id=None
        )
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["message"],
            f"unclaimed {str(path)} (status: open, deployment_state: ready_to_fire)",
        )
