"""test_queue_triage_prose.py -- `queue-triage scaffold-baton --title` gains a
lossless `--title-file` sibling and refuses a newline-bearing inline value,
per the C16 chunk of
state/dispatch-briefs/2026-08-31-prose-flags-travel-as-files-through-the/.

WHY THIS MATTERS. A generated `.cmd` launcher forwards argv as an
un-re-quoted `%*`; cmd.exe truncates its own command line at the first LF
during its own parse, before this CLI ever runs. `--title` is declared
`default=None` on the `scaffold-baton` subparser -- it is OPTIONAL, so the
resolver here is `resolve_optional_prose` (returns None when both the
inline and file forms are absent), NOT `resolve_body` (whose
exactly-one-required semantics would turn an absent title into a usage
error this verb has never raised).

NEGATIVE SPEC -- what must NOT regress:
  - A newline-bearing inline `--title` exits non-zero (parser.error -> 2)
    and never reaches `route_mutation` -- nothing partially dispatched.
  - `--title` absent (and `--title-file` absent) still resolves to `None`
    and the subcommand proceeds exactly as before this change (no `title`
    key forced into the dispatched params).
  - A single-line inline `--title` still works unchanged.

Run: python -m pytest coordinator/bin/tests/test_queue_triage_prose.py -q
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent

_MULTILINE_TITLE = "first line\nsecond line"


def _load_cli_module():
    lib_dir = str(_BIN_DIR / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    engine_root = str(_REPO_ROOT)
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    spec = importlib.util.spec_from_file_location(
        "queue_triage_prose_test", _BIN_DIR / "queue-triage.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod._bootstrap_engine()
    return mod


class _QueueTriageProseTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli_module()

    def setUp(self):
        self._orig_route_mutation = self.cli.route_mutation
        self.addCleanup(self._restore)
        self.calls: list[tuple] = []

        def _fake_route_mutation(op, params, repo_root, legacy_fn):
            self.calls.append((op, params, repo_root))
            return {"ok": True}

        self.cli.route_mutation = _fake_route_mutation

    def _restore(self):
        self.cli.route_mutation = self._orig_route_mutation

    def _write(self, text: str) -> str:
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline=""
        )
        fh.write(text)
        fh.close()
        self.addCleanup(lambda: Path(fh.name).unlink(missing_ok=True))
        return fh.name

    def _run(self, extra: list[str]) -> int:
        return self.cli.main(
            [
                "scaffold-baton", "misc",
                "--entry-path", "plainfile.md",
                "--repo-root", str(_REPO_ROOT),
                *extra,
            ]
        )


class MultilineInlineTitleIsRefusedTest(_QueueTriageProseTestBase):
    def test_multiline_inline_title_exits_nonzero_and_never_dispatches(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--title", _MULTILINE_TITLE])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(self.calls, [])


class TitleFileRoundTripsByteForByteTest(_QueueTriageProseTestBase):
    def test_title_file_sibling_carries_multiline_text_verbatim(self):
        path = self._write(_MULTILINE_TITLE)
        rc = self._run(["--title-file", path])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        _op, params, _repo_root = self.calls[0]
        self.assertEqual(params["title"], _MULTILINE_TITLE)


class AbsentTitleDefaultsToNoneTest(_QueueTriageProseTestBase):
    def test_absent_title_proceeds_with_no_title_key(self):
        rc = self._run([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        _op, params, _repo_root = self.calls[0]
        self.assertNotIn("title", params)


class InlineSingleLineTitleStillWorksTest(_QueueTriageProseTestBase):
    def test_single_line_inline_title_round_trips(self):
        rc = self._run(["--title", "Explicit Title"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        _op, params, _repo_root = self.calls[0]
        self.assertEqual(params["title"], "Explicit Title")


if __name__ == "__main__":
    unittest.main()
