"""test_archive_stamp_cli_chain_supersede_archive.py — argv-parsing unit test
for `archive-stamp-cli chain-archive-handoff` / `supersede-archive-handoff`
(C8, cockpit §6.2 — the two CLI-reachability gaps: 'chain' and
supersede-with-continued_into/exclude were reachable in-process via
handoff.archive_transition but had no archive-stamp-cli verb).

Same idiom as test_archive_stamp_cli_ship_handoff.py: monkeypatches the
`_import_module()` seam with a recording stand-in, so this suite asserts
ONLY the argv -> cs_chain_archive_handoff/cs_supersede_archive_handoff
call-shape translation, never the engine behind it (that is
coordinator_core/test_archive_stamp.py's TestChainArchiveHandoff /
TestSupersedeArchiveHandoff job, and coordinator_core/ops/tests/
test_handoff_archive_transition.py's).

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_chain_supersede_archive.py -v

Renamed from the hyphenated, pytest-uncollectable
test-archive-stamp-cli-chain-supersede-archive.py to this test_* filename;
test bodies unchanged (already unittest.TestCase).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_chain_supersede_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_chain_supersede_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    kwargs each new verb's underlying function was called with."""

    def __init__(self):
        self.chain_calls: list[dict] = []
        self.supersede_calls: list[dict] = []

    def cs_chain_archive_handoff(self, handoff_path, exclude=None):
        self.chain_calls.append({"handoff_path": handoff_path, "exclude": exclude})
        return 0

    def cs_supersede_archive_handoff(self, handoff_path, continued_into, exclude=None):
        self.supersede_calls.append(
            {
                "handoff_path": handoff_path,
                "continued_into": continued_into,
                "exclude": exclude,
            }
        )
        return 0


class _StubHarness(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module


class ChainArchiveHandoffArgvParsingTest(_StubHarness):
    def test_bare_path_no_flags(self):
        rc = _cli.main(["chain-archive-handoff", "state/handoffs/h.md"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.chain_calls[-1],
            {"handoff_path": "state/handoffs/h.md", "exclude": None},
        )

    def test_single_exclude_flag(self):
        rc = _cli.main(
            ["chain-archive-handoff", "state/handoffs/h.md", "--exclude", "state/handoffs/x.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.chain_calls[-1],
            {"handoff_path": "state/handoffs/h.md", "exclude": ["state/handoffs/x.md"]},
        )

    def test_repeatable_exclude_flags_all_forwarded(self):
        rc = _cli.main(
            [
                "chain-archive-handoff",
                "state/handoffs/h.md",
                "--exclude",
                "state/handoffs/x.md",
                "--exclude",
                "state/handoffs/y.md",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.chain_calls[-1]["exclude"],
            ["state/handoffs/x.md", "state/handoffs/y.md"],
        )

    def test_exclude_missing_value_is_usage_error(self):
        rc = _cli.main(["chain-archive-handoff", "state/handoffs/h.md", "--exclude"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.chain_calls, [])

    def test_missing_handoff_path_is_usage_error(self):
        rc = _cli.main(["chain-archive-handoff"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.chain_calls, [])


class SupersedeArchiveHandoffArgvParsingTest(_StubHarness):
    def test_continued_into_required(self):
        rc = _cli.main(["supersede-archive-handoff", "state/handoffs/h.md"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.supersede_calls, [])

    def test_continued_into_forwarded(self):
        rc = _cli.main(
            [
                "supersede-archive-handoff",
                "state/handoffs/h.md",
                "--continued-into",
                "state/handoffs/successor.md",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.supersede_calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "continued_into": "state/handoffs/successor.md",
                "exclude": None,
            },
        )

    def test_continued_into_and_repeatable_exclude(self):
        rc = _cli.main(
            [
                "supersede-archive-handoff",
                "state/handoffs/h.md",
                "--continued-into",
                "state/handoffs/successor.md",
                "--exclude",
                "state/handoffs/x.md",
                "--exclude",
                "state/handoffs/y.md",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.supersede_calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "continued_into": "state/handoffs/successor.md",
                "exclude": ["state/handoffs/x.md", "state/handoffs/y.md"],
            },
        )

    def test_continued_into_missing_value_is_usage_error(self):
        rc = _cli.main(
            ["supersede-archive-handoff", "state/handoffs/h.md", "--continued-into"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.supersede_calls, [])

    def test_exclude_missing_value_is_usage_error(self):
        rc = _cli.main(
            [
                "supersede-archive-handoff",
                "state/handoffs/h.md",
                "--continued-into",
                "state/handoffs/successor.md",
                "--exclude",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.supersede_calls, [])

    def test_missing_handoff_path_is_usage_error(self):
        rc = _cli.main(["supersede-archive-handoff"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.supersede_calls, [])

