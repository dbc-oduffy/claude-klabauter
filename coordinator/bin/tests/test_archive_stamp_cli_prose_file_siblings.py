"""test_archive_stamp_cli_prose_file_siblings.py — the prose-bearing flags of
`archive-stamp-cli` carry a lossless `--<flag>-file <path>` leg, and refuse the
lossy inline form rather than truncating it.

WHAT THIS PINS, AND WHY IT IS NOT A STYLE PREFERENCE. A generated `.cmd`
launcher forwards argv as an un-re-quoted `%*`, and cmd.exe truncates its whole
command line at the first LF during its own parse — before the launcher body,
let alone this CLI, has run. Example-cockpit-repo-em measured the consequence live:
`archive-stamp-cli.cmd correct-handoff-body --old-string <one line>
--new-string <20 lines>` exited 0, printed "applied body correction", and wrote
line 1 of the replacement glued onto the text it was meant to replace. The
corrupted file was committed and reported as done
(`cross-repo/archive/2026-08-21-example-cockpit-repo-em-cmd-wrapper-eats-argv-and-
wsc-tail-exit-3-hides-a-landed-commit.md` § 1).

The remedy is `docs/wiki/windows-first-class.md`'s standing ruling — a payload
that may contain a quote, a space, or a newline travels as a file path, not as
a command-line argument — implemented through the shared
`coordinator_core.argv_fidelity` seam, NOT through raw-cmdline enrolment. The
rejected alternative and its three measured grounds are recorded beside
`_resolve_prose` in the CLI itself; this suite does not restate them.

NEGATIVE SPEC — what must NOT regress, each with its own case below:
  - An empty `--new-string` stays legal. It is how a correction DELETES the
    matched region, and it worked before the file sibling existed; a transport
    fix that quietly made it a usage error would be a behaviour regression.
  - An empty `--old-string` stays illegal (it matches nothing).
  - `--old-string` and `--old-string-file` together are a usage error, never a
    silent precedence rule.
  - The inline form still works for a single-line value. The file leg is
    additive; nothing that worked before this change stops working.

WHAT THIS SUITE DELIBERATELY DOES NOT CLAIM. A single-line value carrying a
quote or a space is still corrupted by `%*` on a real Windows `.cmd` leg, and
no assertion in this process can observe it — the bytes are gone before the
first line of the CLI runs. These are argv-level parse tests; the end-to-end
`.cmd` transport oracle lives in
`coordinator_core/test_bin_launcher_parity.py`.

The `_import_module()` seam is monkeypatched (same idiom as
test_archive_stamp_cli_unclaim_handoff.py) so this suite never needs the engine
root to resolve.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_prose_file_siblings.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    name = "archive_stamp_cli_prose_file_siblings_test"
    loader = importlib.machinery.SourceFileLoader(
        name, str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_MULTILINE = "first line\nsecond line\nthird line"


class _RecordingMod:
    """Stand-in for coordinator_core.archive_stamp, recording the exact
    argument tuples each verb was translated into."""

    def __init__(self):
        self.correct_calls: list[tuple] = []
        self.repair_shipped_calls: list[tuple] = []
        self.repair_state_calls: list[tuple] = []
        self.unclaim_calls: list[tuple] = []

    def cs_correct_handoff_body(self, path, old, new):
        self.correct_calls.append((path, old, new))
        return 0

    def cs_repair_archived_shipped_in(self, path, reason, *, sha=None, unset=False):
        self.repair_shipped_calls.append((path, reason, sha, unset))
        return 0

    def cs_repair_archived_deployment_state(self, path, reason, state, **kwargs):
        self.repair_state_calls.append((path, reason, state, kwargs))
        return 0

    def cs_unclaim_handoff(self, path, note, reaped_from):
        self.unclaim_calls.append((path, note, reaped_from))
        return 0


class _ProseFlagTestBase(unittest.TestCase):
    def setUp(self):
        self._orig = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig

    def _write(self, text: str) -> str:
        import tempfile

        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline=""
        )
        fh.write(text)
        fh.close()
        self.addCleanup(lambda: Path(fh.name).unlink(missing_ok=True))
        return fh.name


class CorrectHandoffBodyTest(_ProseFlagTestBase):
    def test_inline_single_line_still_works(self):
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string", "before", "--new-string", "after",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.correct_calls[-1], ("state/handoffs/h.md", "before", "after")
        )

    def test_multiline_new_string_is_refused_not_truncated(self):
        """The measured incident, inverted into an assertion: the value that
        silently became its own first line must now be a hard refusal naming
        the file sibling — and NOTHING may reach the op.

        --new-string is the arm asserted because it is the less obvious one:
        it carries allow_empty=True, so this pins that permitting an EMPTY
        value did not also permit a truncated one. --old-string reaches the
        identical `_resolve_prose` -> `refuse_newline_argv` path with no
        allow_empty, and had a test asserting exactly this against exactly
        that path."""
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string", "before", "--new-string", _MULTILINE,
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.correct_calls, [])

    def test_file_siblings_carry_multiline_text_byte_for_byte(self):
        old_path = self._write(_MULTILINE)
        new_path = self._write(_MULTILINE.upper())
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string-file", old_path, "--new-string-file", new_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.correct_calls[-1],
            ("state/handoffs/h.md", _MULTILINE, _MULTILINE.upper()),
        )

    def test_inline_and_file_together_are_a_usage_error(self):
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string", "before", "--old-string-file", self._write("x"),
                "--new-string", "after",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.correct_calls, [])

    def test_empty_new_string_stays_legal(self):
        """Deleting the matched region is what an empty replacement MEANS
        here. `allow_empty=True` at that one call site is the reason; if this
        case ever goes red, a transport fix has silently changed the verb."""
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string", "before", "--new-string", "",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.correct_calls[-1], ("state/handoffs/h.md", "before", "")
        )

    def test_empty_old_string_stays_illegal(self):
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string", "", "--new-string", "after",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.correct_calls, [])

    def test_missing_both_forms_is_a_usage_error(self):
        rc = _cli.main(
            ["correct-handoff-body", "state/handoffs/h.md", "--new-string", "after"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.correct_calls, [])

    def test_unreadable_file_refuses_rather_than_proceeding_empty(self):
        rc = _cli.main(
            [
                "correct-handoff-body", "state/handoffs/h.md",
                "--old-string-file", str(_BIN_DIR / "no-such-file-here.txt"),
                "--new-string", "after",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.correct_calls, [])


class RepairReasonTest(_ProseFlagTestBase):
    def test_shipped_in_reason_file_sibling(self):
        rc = _cli.main(
            [
                "repair-archived-shipped-in", "archive/handoffs/h.md",
                "--reason-file", self._write("a reason\nspanning lines"),
                "--unset",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.repair_shipped_calls[-1],
            ("archive/handoffs/h.md", "a reason\nspanning lines", None, True),
        )

    def test_shipped_in_multiline_inline_reason_is_refused(self):
        rc = _cli.main(
            [
                "repair-archived-shipped-in", "archive/handoffs/h.md",
                "--reason", _MULTILINE, "--unset",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.repair_shipped_calls, [])

    def test_deployment_state_reason_file_sibling(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state", "archive/handoffs/h.md",
                "--reason-file", self._write("multi\nline reason"),
                "--deployment-state", "shipped",
            ]
        )
        self.assertEqual(rc, 0)
        path, reason, state, _kwargs = self.stub.repair_state_calls[-1]
        self.assertEqual((path, reason, state),
                         ("archive/handoffs/h.md", "multi\nline reason", "shipped"))

    def test_deployment_state_multiline_inline_reason_is_refused(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state", "archive/handoffs/h.md",
                "--reason", _MULTILINE, "--deployment-state", "shipped",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.repair_state_calls, [])

    def test_inline_single_line_reason_still_works(self):
        rc = _cli.main(
            [
                "repair-archived-shipped-in", "archive/handoffs/h.md",
                "--reason", "a one-line reason", "--unset",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.repair_shipped_calls[-1],
            ("archive/handoffs/h.md", "a one-line reason", None, True),
        )


class UnclaimNoteFileTest(_ProseFlagTestBase):
    def test_note_file_survives_the_leftover_flag_guard(self):
        """`unclaim-handoff` hard-refuses any leftover `--flag` in its tail,
        because an unrecognized flag used to become the park note verbatim.
        `--note-file` must be stripped BEFORE that guard — otherwise the guard
        rejects the very escape its own usage line now advertises."""
        rc = _cli.main(
            [
                "unclaim-handoff", "state/handoffs/h.md",
                "--note-file", self._write("a note\nover two lines"),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.unclaim_calls[-1],
            ("state/handoffs/h.md", "a note\nover two lines", None),
        )

    def test_note_file_composes_with_reaped_from(self):
        rc = _cli.main(
            [
                "unclaim-handoff", "state/handoffs/h.md",
                "--note-file", self._write("reaped note"),
                "--reaped-from", "sid1",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.unclaim_calls[-1],
            ("state/handoffs/h.md", "reaped note", "sid1"),
        )

    def test_positional_note_and_note_file_together_are_a_usage_error(self):
        rc = _cli.main(
            [
                "unclaim-handoff", "state/handoffs/h.md", "inline note",
                "--note-file", self._write("file note"),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.unclaim_calls, [])

    def test_repeated_note_file_is_a_usage_error(self):
        rc = _cli.main(
            [
                "unclaim-handoff", "state/handoffs/h.md",
                "--note-file", self._write("one"),
                "--note-file", self._write("two"),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.unclaim_calls, [])

    def test_multiline_positional_note_is_refused(self):
        rc = _cli.main(
            ["unclaim-handoff", "state/handoffs/h.md", "note\nwith a newline"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.unclaim_calls, [])

    def test_single_line_positional_note_still_works(self):
        rc = _cli.main(["unclaim-handoff", "state/handoffs/h.md", "a plain note"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.unclaim_calls[-1], ("state/handoffs/h.md", "a plain note", None)
        )


class UsageDeclaresEveryFileSiblingTest(unittest.TestCase):
    """`_reject_unknown_flags` derives its accepted set FROM `_SUBCOMMAND_USAGE`
    — a flag the parser honours but the usage line never names is refused at
    the door. So every `-file` sibling wired above must appear in its verb's
    usage row, and this asserts it directly rather than trusting that the
    round-trip cases above happened to cover it."""

    CASES = [
        ("correct-handoff-body", ("--old-string-file", "--new-string-file")),
        ("repair-archived-shipped-in", ("--reason-file",)),
        ("repair-archived-deployment-state", ("--reason-file",)),
        ("unclaim-handoff", ("--note-file",)),
        ("unconsume-handoff", ("--note-file",)),
    ]

    def test_every_file_sibling_is_declared(self):
        for verb, flags in self.CASES:
            usage = _cli._SUBCOMMAND_USAGE[verb]
            for flag in flags:
                with self.subTest(verb=verb, flag=flag):
                    self.assertIn(flag, usage)


class DispositionNoteFileSiblingTest(_ProseFlagTestBase):
    """The three prose-bearing disposition flags of `action-memo`/`resolve-memo`
    carry the same file sibling as every other prose-bearing flag in this CLI.

    WHY THESE THREE AND NOT THE WHOLE TAIL. `action-memo` forwards its tail to
    the engine verbatim — `_DISPOSITION_FLAGS` is the declaration, not this
    file — so the sibling is resolved and NORMALISED BACK to the inline form
    before dispatch. `--decision`, `--realized-by`, `--superseded-by` and the
    rest are pointers and enum values, not prose; they carry no newline, quote
    or space exposure and get no sibling.

    NEGATIVE SPEC:
      - The file leg is NOT a multi-line channel. A multi-line note reaches the
        engine intact and is refused there by `_validate_disposition`, because
        `serialize_yaml_scalar` emits an inline scalar. This CLI does not
        pre-empt that refusal and does not weaken it.
      - No `-file` token ever reaches the op.
      - A repeated flag (either form) is a usage error, never a silent
        first-wins/last-wins flip.
    """

    def setUp(self):
        super().setUp()
        self.stub.action_calls: list[tuple] = []
        self.stub.resolve_calls: list[tuple] = []
        self.stub.cs_action_memo = lambda path, *tail: (
            self.stub.action_calls.append((path, tail)) or 0
        )
        self.stub.cs_resolve_memo = lambda path, *tail: (
            self.stub.resolve_calls.append((path, tail)) or 0
        )

    def test_decision_note_file_survives_interleaving_with_other_flags(self):
        # Review: overengineering-reviewer -- trimmed to the one fact this case
        # uniquely pins (resolution survives interleaving with unrelated
        # flags); the resolved-tail/no-`-file`-token facts are already covered
        # by test_every_prose_disposition_flag_has_a_working_sibling.
        note_path = self._write("a note with 'quotes' and spaces")
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision", "accepted", "--realized-by", "abc1234",
                "--decision-note-file", note_path,
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail[tail.index("--decision") + 1], "accepted")
        self.assertEqual(tail[tail.index("--realized-by") + 1], "abc1234")

    def test_every_prose_disposition_flag_has_a_working_sibling(self):
        for flag in _cli._PROSE_DISPOSITION_FLAGS:
            with self.subTest(flag=flag):
                note_path = self._write(f"note for {flag}")
                rc = _cli.main(
                    ["resolve-memo", "cross-repo/inbox/m.md", f"{flag}-file", note_path]
                )
                self.assertEqual(rc, 0)
                _, tail = self.stub.resolve_calls[-1]
                self.assertEqual(tail, (flag, f"note for {flag}"))

    def test_inline_form_is_forwarded_unchanged(self):
        rc = _cli.main(
            ["action-memo", "cross-repo/inbox/m.md", "--decision-note", "one line"]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--decision-note", "one line"))

    def test_inline_and_file_together_are_a_usage_error(self):
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision-note", "inline",
                "--decision-note-file", self._write("from file"),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.action_calls, [])

    def test_repeated_flag_is_refused_not_silently_collapsed(self):
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision-note", "first", "--decision-note", "second",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.action_calls, [])

    def test_multiline_inline_note_is_refused_here(self):
        rc = _cli.main(
            ["action-memo", "cross-repo/inbox/m.md", "--decision-note", _MULTILINE]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.action_calls, [])

    def test_multiline_file_note_reaches_the_engine_for_its_own_refusal(self):
        """The file leg does not smuggle a multi-line note past the engine, and
        this CLI does not duplicate the engine's refusal either. The value
        arrives intact; `_validate_disposition` is the one that says no."""
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision-note-file", self._write(_MULTILINE),
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--decision-note", _MULTILINE))

    def test_prose_flags_are_all_real_engine_disposition_flags(self):
        """`_PROSE_DISPOSITION_FLAGS` is a SUBSET selection over the engine's
        table, not a second declaration of it. If a flag is renamed engine-side
        the sibling silently stops applying to anything, so the membership is
        pinned rather than assumed."""
        from coordinator_core.archive_stamp import _DISPOSITION_FLAGS

        for flag in _cli._PROSE_DISPOSITION_FLAGS:
            self.assertIn(flag, _DISPOSITION_FLAGS)

    def test_unrelated_disposition_flags_are_untouched(self):
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--superseded-by", "cross-repo/archive/other.md",
                "--correct-realization",
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(
            tail,
            ("--superseded-by", "cross-repo/archive/other.md", "--correct-realization"),
        )

    def test_a_note_whose_text_is_a_flag_name_is_not_re_read_as_a_flag(self):
        """The membership-scan trap the positional walk exists to avoid.

        `--decision-note --actioned-note` is a note whose literal text is
        another flag's name. `_parse_disposition_args` consumes it as a VALUE,
        so anything here that scanned by membership would strip a pair the
        engine never saw and change what lands."""
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision-note", "--actioned-note",
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--decision-note", "--actioned-note"))

    def test_a_trailing_prose_flag_with_no_value_is_refused(self):
        rc = _cli.main(["action-memo", "cross-repo/inbox/m.md", "--decision-note"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.action_calls, [])

    # Review: coordinator:code-reviewer a5c86ae1f7c7c0a12 -- Finding 1/2. Before
    # the fix, the positional walk tracked "consumed as a value" only for the
    # three prose flags, so a non-prose 2-token flag's value (or missing-value
    # slot) landing on a prose flag's name got misread as a fresh pair and the
    # tail was silently rewritten. These pin the walk against the engine's full
    # `_DISPOSITION_FLAGS`/`_DISPOSITION_BOOL_FLAGS` vocabulary, not just the
    # three prose ones.

    def test_a_non_prose_flags_value_that_looks_like_a_prose_flag_is_untouched(self):
        """`--realized-by`'s VALUE is literally `--decision-note`. The walk must
        consume it as `--realized-by`'s value, verbatim, never as the start of
        a fresh prose pair."""
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--realized-by", "--decision-note",
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--realized-by", "--decision-note"))

    def test_a_missing_value_before_a_prose_file_flag_is_forwarded_unmangled(self):
        """`--decision` is missing its value; the next token is a prose flag's
        `-file` sibling. The walk must consume `--decision-note-file` as
        `--decision`'s (bogus) value -- exactly what the untouched tail would
        hand the engine -- rather than resolving it as a note and rewriting
        the tail."""
        note_path = self._write("orphaned note")
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--decision", "--decision-note-file", note_path,
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--decision", "--decision-note-file", note_path))

    def test_another_non_prose_flags_value_that_looks_like_a_prose_flag_is_untouched(self):
        """Same class as above with a different 2-token engine flag
        (`--distill-fate`), so the coverage is about the class, not one flag."""
        rc = _cli.main(
            [
                "action-memo", "cross-repo/inbox/m.md",
                "--distill-fate", "--actioned-note",
            ]
        )
        self.assertEqual(rc, 0)
        _, tail = self.stub.action_calls[-1]
        self.assertEqual(tail, ("--distill-fate", "--actioned-note"))

if __name__ == "__main__":
    unittest.main()
