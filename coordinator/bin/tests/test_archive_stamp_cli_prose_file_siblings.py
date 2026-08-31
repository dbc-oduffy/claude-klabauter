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


if __name__ == "__main__":
    unittest.main()
