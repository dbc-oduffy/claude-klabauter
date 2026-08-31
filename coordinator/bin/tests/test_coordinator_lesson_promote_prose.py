"""test_coordinator_lesson_promote_prose.py -- `coordinator-lesson-promote
--title` gains a lossless `--title-file` sibling and refuses a
newline-bearing inline value, per the C15 chunk of
state/dispatch-briefs/2026-08-31-prose-flags-travel-as-files-through-the/.

WHY THIS MATTERS. A generated `.cmd` launcher forwards argv as an
un-re-quoted `%*`; cmd.exe truncates its own command line at the first LF
during its own parse, before this CLI ever runs. `--title` is declared
`required=True` on this CLI's parser -- it is a REQUIRED flag, so the
resolver here is `resolve_body` (exactly-one-of --title/--title-file
required) via `refuse_newline_argv` first, matching the same shape this
file already uses for `--body`/`--body-file`.

NEGATIVE SPEC -- what must NOT regress:
  - A newline-bearing inline `--title` exits non-zero (parser.error -> 2)
    and names `--title-file`, before any route/write is attempted.
  - The `--title-file` leg round-trips a multi-line value byte-for-byte.
  - A single-line inline `--title` still works unchanged.

Run: python -m pytest coordinator/bin/tests/test_coordinator_lesson_promote_prose.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "coordinator-lesson-promote.py"

_LIB_DIR = _BIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_loader = importlib.machinery.SourceFileLoader(
    "coordinator_lesson_promote_prose_test", str(_CLI_PATH)
)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)  # type: ignore[union-attr]

_MULTILINE_TITLE = "first line\nsecond line"

_FAKE_SCHEMA_OUTPUT = {"enums": {"change_kind": ["doctrine-edit", "wiki-append", "skill-edit"]}}

_BASE_ARGV = [
    "--body", "Test lesson body prose",
    "--change-kind", "doctrine-edit",
    "--target-wiki", "docs/wiki/test-wiki.md",
]


class _LessonPromoteProseTestBase(unittest.TestCase):
    def _write(self, text: str) -> str:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline=""
        )
        fh.write(text)
        fh.close()
        self.addCleanup(lambda: Path(fh.name).unlink(missing_ok=True))
        return fh.name

    def _run(self, extra: list[str]) -> tuple[int, list]:
        fake_result = {"out_path": "/fake/path.yaml"}
        with (
            unittest.mock.patch.object(
                _cli_mod, "_cc_route", return_value=fake_result
            ) as mock_route,
            unittest.mock.patch.object(
                _cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT
            ),
            unittest.mock.patch.object(
                _cli_mod, "_resolve_from_repo", return_value="test-repo-em"
            ),
            unittest.mock.patch.object(
                _cli_mod, "_current_repo_root", return_value="/fake/repo"
            ),
            unittest.mock.patch("sys.stdout", io.StringIO()),
        ):
            rc = _cli_mod.main([*extra, *_BASE_ARGV])
            calls = mock_route.call_args_list
        return rc, calls


class MultilineInlineTitleIsRefusedTest(_LessonPromoteProseTestBase):
    def test_multiline_inline_title_exits_nonzero_and_never_routes(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--title", _MULTILINE_TITLE])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_multiline_inline_title_error_names_title_file(self):
        stderr = io.StringIO()
        with (
            unittest.mock.patch("sys.stderr", stderr),
            self.assertRaises(SystemExit),
        ):
            self._run(["--title", _MULTILINE_TITLE])
        self.assertIn("--title-file", stderr.getvalue())


class TitleFileRoundTripsByteForByteTest(_LessonPromoteProseTestBase):
    def test_title_file_sibling_carries_multiline_text_verbatim(self):
        path = self._write(_MULTILINE_TITLE)
        rc, calls = self._run(["--title-file", path])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        _op, params = calls[0][0][0], calls[0][0][1]
        self.assertEqual(params["title"], _MULTILINE_TITLE)


class InlineSingleLineTitleStillWorksTest(_LessonPromoteProseTestBase):
    def test_single_line_inline_title_round_trips(self):
        rc, calls = self._run(["--title", "Explicit Title"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        _op, params = calls[0][0][0], calls[0][0][1]
        self.assertEqual(params["title"], "Explicit Title")


if __name__ == "__main__":
    unittest.main()
