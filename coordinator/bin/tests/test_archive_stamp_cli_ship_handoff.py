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

