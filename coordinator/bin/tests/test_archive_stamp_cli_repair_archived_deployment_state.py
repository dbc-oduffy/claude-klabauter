"""test_archive_stamp_cli_repair_archived_deployment_state.py — argv-parsing
unit test for `archive-stamp-cli repair-archived-deployment-state` (2026-07-26).

Defect this closes: `ship-handoff`'s state/handoffs/-only containment refuses
archive/handoffs/ paths, so 13 archived handoffs stuck at
deployment_state: in_flight could not be repaired through any existing verb
and were instead hand-edited (DoE-claude cross-repo memo, 2026-07-26). This
suite covers the CLI veneer's argv -> `cs_repair_archived_deployment_state(...)`
call-shape translation only — not the engine behind it (that is
coordinator_core/ops/tests/test_handoff_stamp.py's job, which exercises the
handler's frontmatter-write/cross-field-validation contract directly).

The `_import_module()` seam is monkeypatched (same idiom as
test_archive_stamp_cli_close_handoff.py) so this suite never requires
CLAUDE_KLABAUTER_ROOT to resolve or coordinator_core to be importable.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
archive-stamp-cli is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as the other archive-stamp-cli argv-parsing suites.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_repair_archived_deployment_state.py -v
"""
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
    """Stand-in for coordinator_core.archive_stamp — records the exact kwargs
    cs_repair_archived_deployment_state was called with, so each test can
    assert the argv -> call-shape translation without a real claude-klabauter checkout."""

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
        """--continued-into-override is a bare flag (no value) that must
        forward continued_into_override=True — the escape valve for a
        successor recovered from git history / a cross-repo reference this
        single-repo CLI cannot verify."""
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
        """A CLI-layer PASS (reason + deployment-state supplied) must still
        propagate an engine-layer refusal (e.g. continued with no
        continued_into) rather than masking it — the CLI does NOT re-validate
        the cross-field rule itself; the handler is the single authoritative
        gate for that."""

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
