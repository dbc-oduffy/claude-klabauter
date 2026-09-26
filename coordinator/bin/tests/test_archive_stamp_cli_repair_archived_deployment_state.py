from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_repair_archived_deployment_state_test",
        str(_BIN_DIR / "archive-stamp-cli.py"),
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_repair_archived_deployment_state_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingRepairMod:

    def __init__(self):
        self.calls: list[dict] = []

    def cs_repair_archived_deployment_state(
        self,
        handoff_path,
        reason,
        deployment_state,
        continued_into=None,
        continued_into_override=False,
        closed_reason=None,
    ):
        self.calls.append(
            {
                "handoff_path": handoff_path,
                "reason": reason,
                "deployment_state": deployment_state,
                "continued_into": continued_into,
                "continued_into_override": continued_into_override,
                "closed_reason": closed_reason,
            }
        )
        return 0


class RepairArchivedDeploymentStateArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingRepairMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_reason_and_deployment_state_forwarded(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "stuck in_flight",
                "--deployment-state",
                "shipped",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "archive/handoffs/2026-07/h.md",
                "reason": "stuck in_flight",
                "deployment_state": "shipped",
                "continued_into": None,
                "continued_into_override": False,
                "closed_reason": None,
            },
        )

    def test_continued_into_forwarded(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "succession proof recovered",
                "--deployment-state",
                "continued",
                "--continued-into",
                "hnd-successor-abc123",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["continued_into"], "hnd-successor-abc123")
        self.assertEqual(self.stub.calls[-1]["deployment_state"], "continued")
        self.assertEqual(self.stub.calls[-1]["continued_into_override"], False)

    def test_continued_into_override_flag_forwarded(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "git-history-recovered: deleted by distill sweep",
                "--deployment-state",
                "continued",
                "--continued-into",
                "hnd-deleted-successor-abc999",
                "--continued-into-override",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["continued_into_override"], True)
        self.assertEqual(self.stub.calls[-1]["continued_into"], "hnd-deleted-successor-abc999")

    def test_closed_reason_forwarded(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "deliberate stop",
                "--deployment-state",
                "closed",
                "--closed-reason",
                "stale",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["closed_reason"], "stale")

    def test_missing_reason_is_usage_error_no_engine_call(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--deployment-state",
                "shipped",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_missing_deployment_state_is_usage_error_no_engine_call(self):
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "no target state",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_missing_handoff_path_is_usage_error_no_engine_call(self):
        rc = _cli.main(["repair-archived-deployment-state"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_engine_refusal_propagates_verbatim(self):

        class _RefusingMod:
            def cs_repair_archived_deployment_state(
                self,
                handoff_path,
                reason,
                deployment_state,
                continued_into=None,
                continued_into_override=False,
                closed_reason=None,
            ):
                return 1

        _cli._import_module = lambda: _RefusingMod()
        rc = _cli.main(
            [
                "repair-archived-deployment-state",
                "archive/handoffs/2026-07/h.md",
                "--reason",
                "test",
                "--deployment-state",
                "continued",
            ]
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
