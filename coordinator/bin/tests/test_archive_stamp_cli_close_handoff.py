from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_close_handoff_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_close_handoff_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingCloseHandoffMod:

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

        class _RefusingMod:
            def cs_close_handoff(self, handoff_path, reason):
                return 1

        _cli._import_module = lambda: _RefusingMod()
        rc = _cli.main(
            ["close-handoff", "state/handoffs/h.md", "--reason", "not-a-real-reason"]
        )
        self.assertEqual(rc, 1)

