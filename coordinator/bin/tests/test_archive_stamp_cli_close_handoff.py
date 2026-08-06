"""test_archive_stamp_cli_close_handoff.py — argv-parsing unit test for
`archive-stamp-cli close-handoff` (2026-07-25, plan C10).

Defect this closes: DR-084 (2026-07-22) widened the handoff terminal
vocabulary to add deployment_state:closed + a required closed_reason
(cancelled|displaced|stale), but no archive-stamp-cli subcommand could write
it. Three days later an executor tried to close a genuinely dead baton
(roadmap-lvv-07) and, lacking a correct verb, archived it via
chain-archive-handoff with a zero-byte frontmatter diff — leaving an
archived handoff still reading status: open, which
handoff-archived.schema.json does not admit. Reverted in commit f145480d.

The `_import_module()` seam is monkeypatched (same idiom as
test_archive_stamp_cli_ship_handoff.py) so this suite never requires
CLAUDE_KLABAUTER_ROOT to resolve or coordinator_core to be importable — it asserts
ONLY the argv -> `cs_close_handoff(...)` call-shape translation, not the
engine behind it (that is
coordinator_core/ops/tests/test_handoff_transition.py's job, which exercises
the `close` verb's frontmatter-write/validation/idempotency contract
directly).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
archive-stamp-cli is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as test_archive_stamp_cli_ship_handoff.py.

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C10;
state/roadmap/lifecycle-vocab/cutovers/closed-reason-terminal.md.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_close_handoff.py -v

Renamed from the hyphenated, pytest-uncollectable
test-archive-stamp-cli-close-handoff.py to this test_* filename; test
bodies unchanged (already unittest.TestCase).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_close_handoff_test", str(_BIN_DIR / "archive-stamp-cli")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_close_handoff_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingCloseHandoffMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    kwargs cs_close_handoff was called with, so each test can assert the
    argv -> call-shape translation without a real claude-klabauter checkout."""

    def __init__(self):
        self.calls: list[dict] = []

    def cs_close_handoff(self, handoff_path, reason):
        self.calls.append({"handoff_path": handoff_path, "reason": reason})
        return 0


class CloseHandoffArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingCloseHandoffMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_reason_flag_is_forwarded(self):
        rc = _cli.main(
            ["close-handoff", "state/handoffs/h.md", "--reason", "stale"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {"handoff_path": "state/handoffs/h.md", "reason": "stale"},
        )

    def test_each_enum_value_is_forwarded_verbatim(self):
        for reason in ("cancelled", "displaced", "stale"):
            with self.subTest(reason=reason):
                self.stub.calls.clear()
                rc = _cli.main(
                    ["close-handoff", "state/handoffs/h.md", "--reason", reason]
                )
                self.assertEqual(rc, 0)
                self.assertEqual(self.stub.calls[-1]["reason"], reason)

    def test_missing_reason_flag_is_usage_error_no_engine_call(self):
        rc = _cli.main(["close-handoff", "state/handoffs/h.md"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_reason_flag_missing_value_is_usage_error_no_engine_call(self):
        rc = _cli.main(["close-handoff", "state/handoffs/h.md", "--reason"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_missing_handoff_path_is_usage_error_no_engine_call(self):
        rc = _cli.main(["close-handoff"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_engine_refusal_propagates_verbatim(self):
        """A CLI-layer PASS (reason supplied) must still propagate an
        engine-layer refusal (e.g. an out-of-enum reason, or a conflicting
        shipped/continued terminal) rather than masking it — the CLI does
        NOT re-validate the enum itself; cs_close_handoff/the close verb is
        the single authoritative gate for that."""

        class _RefusingMod:
            def cs_close_handoff(self, handoff_path, reason):
                return 1

        _cli._import_module = lambda: _RefusingMod()
        rc = _cli.main(
            ["close-handoff", "state/handoffs/h.md", "--reason", "not-a-real-reason"]
        )
        self.assertEqual(rc, 1)

