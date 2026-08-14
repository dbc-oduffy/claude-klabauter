"""test_archive_stamp_cli_ship_handoff.py — argv-parsing unit test for
`archive-stamp-cli ship-handoff` (2026-07-22 fix).

Defect 1 this closes: the CLI veneer parsed NO sha at all — a caller passing
a positional sha (`ship-handoff <path> <sha>`) or a `--sha <sha>` flag had it
silently swallowed, even though the engine (`cs_ship_handoff` /
`handoff.archive_transition`) already accepted and threaded a caller-supplied
sha. This was a veneer-only gap (the capability existed and was unreachable
from the CLI) — this suite is the missing coverage: EVERY prior test for
this incident exercised `arstamp.cs_ship_handoff(...)` as a Python function
call, never the trampoline's own `main()` argv parsing.

The `_import_module()` seam is monkeypatched (same idiom as
test_session_claim_cli.py) so this suite never requires CLAUDE_KLABAUTER_ROOT to
resolve or `coordinator_core` to be importable — it asserts ONLY the argv ->
`cs_ship_handoff(...)` call-shape translation, not the engine behind it
(that is `coordinator_core/ops/tests/test_handoff_archive_transition.py`'s
and `coordinator_core/test_archive_stamp.py`'s job).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`archive-stamp-cli` is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as test_session_claim_cli.py.

Spec backlink: incident "archive-stamp-cli ship-handoff cannot land" (2026-07-22),
cross-repo memo 2026-07-22-claude-central-em-deliver-ship-handoff-writes-
deployment-state-shipped-without-shipped-in.md.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_ship_handoff.py -v

Renamed from the hyphenated, pytest-uncollectable
test-archive-stamp-cli-ship-handoff.py to this test_* filename; test
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
        "archive_stamp_cli_ship_handoff_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_ship_handoff_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingShipHandoffMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    kwargs cs_ship_handoff was called with, so each test can assert the argv
    -> call-shape translation without a real claude-klabauter checkout."""

    def __init__(self):
        self.calls: list[dict] = []

    def cs_ship_handoff(self, handoff_path, archive=False, sha=None, force=False):
        self.calls.append(
            {"handoff_path": handoff_path, "archive": archive, "sha": sha, "force": force}
        )
        return 0


class ShipHandoffArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingShipHandoffMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_bare_path_no_flags(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {"handoff_path": "state/handoffs/h.md", "archive": False, "sha": None, "force": False},
        )

    def test_bare_positional_sha_is_forwarded(self):
        """Regression test for the incident: `ship-handoff <path> <sha>` must
        thread the sha through, not silently swallow it."""
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "f7c81a1d"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "archive": False,
                "sha": "f7c81a1d",
                "force": False,
            },
        )

    def test_dash_dash_sha_flag_is_forwarded(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "--sha", "f7c81a1d"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "archive": False,
                "sha": "f7c81a1d",
                "force": False,
            },
        )

    def test_dash_dash_sha_missing_value_is_usage_error(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "--sha"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_positional_and_matching_dash_dash_sha_ok(self):
        rc = _cli.main(
            ["ship-handoff", "state/handoffs/h.md", "f7c81a1d", "--sha", "f7c81a1d"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["sha"], "f7c81a1d")

    def test_positional_and_conflicting_dash_dash_sha_fails_loud(self):
        rc = _cli.main(
            ["ship-handoff", "state/handoffs/h.md", "f7c81a1d", "--sha", "deadbeef"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_archive_flag_still_works(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "--archive"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {"handoff_path": "state/handoffs/h.md", "archive": True, "sha": None, "force": False},
        )

    def test_archive_flag_after_positional_sha(self):
        """Regression test for the incident: the prior parser matched
        `--archive` ONLY at the fixed rest[1:2] slot, so a sha preceding
        --archive silently dropped the archive flag."""
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "f7c81a1d", "--archive"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "archive": True,
                "sha": "f7c81a1d",
                "force": False,
            },
        )

    def test_force_flag_is_forwarded(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "--sha", "f7c81a1d", "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "archive": False,
                "sha": "f7c81a1d",
                "force": True,
            },
        )

    def test_unrecognized_second_positional_is_usage_error(self):
        rc = _cli.main(["ship-handoff", "state/handoffs/h.md", "f7c81a1d", "deadbeef"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_missing_handoff_path_is_usage_error(self):
        rc = _cli.main(["ship-handoff"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

