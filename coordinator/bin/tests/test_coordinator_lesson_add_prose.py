"""test_coordinator_lesson_add_prose.py -- `coordinator-lesson-add --title` and
`--why` gain lossless `--title-file` / `--why-file` siblings and refuse a
newline-bearing inline value, per the C14 chunk of
state/dispatch-briefs/2026-08-31-prose-flags-travel-as-files-through-the/.

WHY THIS MATTERS. A generated `.cmd` launcher forwards argv as an
un-re-quoted `%*`; cmd.exe truncates its own command line at the first LF
during its own parse, before this CLI ever runs. `--title` is REQUIRED
(exactly one of `--title` / `--title-file`), so its resolver is
`resolve_body` (mirrors the existing `--body`/`--body-file` shape) --
`resolve_optional_prose`'s "both absent -> None" case would be wrong here,
since an absent title has always been a usage error for this CLI. `--why`
is OPTIONAL (`default=None`), so its resolver is `resolve_optional_prose`.

NEGATIVE SPEC -- what must NOT regress:
  - A newline-bearing inline `--title` exits non-zero (parser.error -> 2)
    and never reaches the dedup pre-check or subprocess delegation.
  - A newline-bearing inline `--why` exits non-zero the same way.
  - `--why` and `--why-file` both absent still resolves to `None` and the
    delegated command carries no `--why` flag, exactly as before this change.
  - A single-line inline `--title` and `--why` still work unchanged.

Run: python -m pytest coordinator/bin/tests/test_coordinator_lesson_add_prose.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import tempfile
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — locate CLI relative to this test file
# test file: coordinator/bin/tests/test_coordinator_lesson_add_prose.py
# CLI:       coordinator/bin/coordinator-lesson-add.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "coordinator-lesson-add.py"

_MULTILINE_TITLE = "first line\nsecond line"
_MULTILINE_WHY = "why line one\nwhy line two"

# Load the CLI as a Python module for unit testing.
_loader = importlib.machinery.SourceFileLoader(
    "coordinator_lesson_add_prose_test", str(_CLI_PATH)
)
_spec = importlib.util.spec_from_loader("coordinator_lesson_add_prose_test", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)  # type: ignore[union-attr]


def _write(text: str) -> str:
    fh = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline=""
    )
    fh.write(text)
    fh.close()
    return fh.name


def _invoke(argv, mock_run):
    """Run main() with patched sys.argv and subprocess.run; return exit code."""
    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", mock_run),
        unittest.mock.patch("sys.argv", ["coordinator-lesson-add"] + argv),
    ):
        try:
            rc = _cli_mod.main()
            return int(rc) if rc is not None else 0
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0


def _ok_mock_run():
    """subprocess.run stub: succeeds unconditionally.

    Patches the single global `subprocess.run`, so it also answers
    `_queue_append_locator.find_cli_cmd`'s own `--help` liveness probe
    (its first call) before the real delegated invocation (its second
    call) -- a successful run therefore calls this mock TWICE, not once.
    """
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    return unittest.mock.MagicMock(return_value=mock_result)


def _delegated_cmd(mock_run):
    """Return the argv of the real delegated call (the last call), after
    asserting the expected probe-then-delegate call shape (2 calls)."""
    assert mock_run.call_count == 2, (
        f"expected probe call + delegated call, got {mock_run.call_count} calls"
    )
    return mock_run.call_args_list[-1][0][0]


class MultilineInlineTitleIsRefusedTest(unittest.TestCase):
    def test_multiline_inline_title_exits_nonzero_and_never_dispatches(self):
        mock_run = _ok_mock_run()
        rc = _invoke(
            ["--title", _MULTILINE_TITLE, "--body", "some body", "--scope", "project"],
            mock_run,
        )
        self.assertNotEqual(rc, 0)
        mock_run.assert_not_called()


class MultilineTitleFileIsRefusedTest(unittest.TestCase):
    """Refused HERE, before the child is spawned -- which is what
    `assert_not_called` pins, not merely "refused somewhere in the pipeline".

    This previously asserted the OPPOSITE (that the wrapper forwards a
    multi-line title verbatim) and passed, because the child is mocked and
    never runs; it pinned a transport the child refuses.
    """

    def test_multiline_title_file_exits_nonzero_and_never_dispatches(self):
        path = _write(_MULTILINE_TITLE)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        mock_run = _ok_mock_run()
        rc = _invoke(
            ["--title-file", path, "--body", "some body", "--scope", "project"],
            mock_run,
        )
        self.assertNotEqual(rc, 0)
        mock_run.assert_not_called()


class SingleLineTitleFileRoundTripsByteForByteTest(unittest.TestCase):
    """The transport itself is unchanged for the case it can actually serve."""

    def test_title_file_sibling_carries_single_line_text_verbatim(self):
        title = "A single-line title from a file, with a unicode em-dash — kept"
        path = _write(title)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        mock_run = _ok_mock_run()
        rc = _invoke(
            ["--title-file", path, "--body", "some body", "--scope", "project"],
            mock_run,
        )
        self.assertEqual(rc, 0)
        cmd = _delegated_cmd(mock_run)
        idx = cmd.index("--title")
        self.assertEqual(cmd[idx + 1], title)


class AbsentTitleIsUsageErrorTest(unittest.TestCase):
    def test_absent_title_and_title_file_exits_nonzero(self):
        mock_run = _ok_mock_run()
        rc = _invoke(["--body", "some body", "--scope", "project"], mock_run)
        self.assertNotEqual(rc, 0)
        mock_run.assert_not_called()


class InlineSingleLineTitleStillWorksTest(unittest.TestCase):
    def test_single_line_inline_title_round_trips(self):
        mock_run = _ok_mock_run()
        rc = _invoke(
            ["--title", "Explicit Title", "--body", "some body", "--scope", "project"],
            mock_run,
        )
        self.assertEqual(rc, 0)
        cmd = _delegated_cmd(mock_run)
        idx = cmd.index("--title")
        self.assertEqual(cmd[idx + 1], "Explicit Title")


class MultilineInlineWhyIsRefusedTest(unittest.TestCase):
    def test_multiline_inline_why_exits_nonzero_and_never_dispatches(self):
        mock_run = _ok_mock_run()
        rc = _invoke(
            [
                "--title", "Some Title", "--body", "some body", "--scope", "project",
                "--why", _MULTILINE_WHY,
            ],
            mock_run,
        )
        self.assertNotEqual(rc, 0)
        mock_run.assert_not_called()


class WhyFileRoundTripsByteForByteTest(unittest.TestCase):
    def test_why_file_sibling_carries_multiline_text_verbatim(self):
        path = _write(_MULTILINE_WHY)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        mock_run = _ok_mock_run()
        rc = _invoke(
            [
                "--title", "Some Title", "--body", "some body", "--scope", "project",
                "--why-file", path,
            ],
            mock_run,
        )
        self.assertEqual(rc, 0)
        cmd = _delegated_cmd(mock_run)
        idx = cmd.index("--why")
        self.assertEqual(cmd[idx + 1], _MULTILINE_WHY)


class AbsentWhyBehavesAsTodayTest(unittest.TestCase):
    def test_absent_why_and_why_file_omits_the_flag(self):
        mock_run = _ok_mock_run()
        rc = _invoke(
            ["--title", "Some Title", "--body", "some body", "--scope", "project"],
            mock_run,
        )
        self.assertEqual(rc, 0)
        cmd = _delegated_cmd(mock_run)
        self.assertNotIn("--why", cmd)


class InlineSingleLineWhyStillWorksTest(unittest.TestCase):
    def test_single_line_inline_why_round_trips(self):
        mock_run = _ok_mock_run()
        rc = _invoke(
            [
                "--title", "Some Title", "--body", "some body", "--scope", "project",
                "--why", "Explicit Why",
            ],
            mock_run,
        )
        self.assertEqual(rc, 0)
        cmd = _delegated_cmd(mock_run)
        idx = cmd.index("--why")
        self.assertEqual(cmd[idx + 1], "Explicit Why")


if __name__ == "__main__":
    unittest.main()
