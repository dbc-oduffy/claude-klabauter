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
CLAUDE_KLABAUTER_ROOT to resolve or `coordinator_core` to be importable.

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
import unittest
from pathlib import Path

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
    """Source-level check (Finding 3's last ask): the reaper's live-children
    guard and indeterminate-guard skip branches must never construct a
    `_run_archive_stamp_cli` call carrying `--reaped-from` — only the single
    release-leg (`unconsume-handoff`) call site may.

    A subprocess-level assertion would require standing up a full reaper
    fixture (frontmatter corpus, live-children guard, dead-holder
    detection); this is deliberately a static scan of the reaper source
    instead, matching how Finding 5 in the same review was itself verified
    (read-based, not execution-based)."""

    def test_exactly_one_reaped_from_call_site_in_reaper(self):
        reaper_src = (_BIN_DIR / "reap-orphaned-in-flight-handoffs.py").read_text()
        call_sites = re.findall(r"_run_archive_stamp_cli\(\s*\[[^\]]*\]", reaper_src)
        with_reaped_from = [c for c in call_sites if "--reaped-from" in c]
        self.assertEqual(
            len(with_reaped_from),
            1,
            "expected exactly one _run_archive_stamp_cli(...) call site "
            "carrying --reaped-from (the release leg); "
            f"found {len(with_reaped_from)}: {with_reaped_from}",
        )
        self.assertIn("unconsume-handoff", with_reaped_from[0])
