from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_claim_handoff_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_claim_handoff_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingClaimHandoffMod:

    def __init__(
        self,
        *,
        claim_rc: int = 0,
        sid: str | None = "sess-abc123",
        writes: dict[str, bool] | None = None,
    ):
        self.calls: list[tuple[str, bool]] = []
        self._claim_rc = claim_rc
        self._sid = sid
        self._writes = writes

    def cs_claim_handoff(self, handoff_path, *, return_result: bool = False):
        self.calls.append((handoff_path, return_result))
        result: dict = {"exit_code": self._claim_rc, "applied": self._claim_rc == 0}
        if self._claim_rc != 0:
            result["error"] = "stub refusal"
        elif self._writes is not None:
            result["writes"] = self._writes
        if return_result:
            return result
        return self._claim_rc

    def resolve_current_session_id(self):
        return self._sid


class ClaimHandoffSuccessLineTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_exit_0_prints_exactly_one_line_naming_path_and_sid(self):
        stub = _RecordingClaimHandoffMod(claim_rc=0, sid="sess-abc123")
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 0)
        self.assertEqual(stub.calls, [("state/handoffs/h.md", True)])
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertIn("state/handoffs/h.md", lines[0])
        self.assertIn("sess-abc123", lines[0])

    def test_deprecated_alias_consume_handoff_also_prints_success_line(self):
        stub = _RecordingClaimHandoffMod(claim_rc=0, sid="sess-xyz789")
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["consume-handoff", "state/handoffs/h2.md"])

        self.assertEqual(rc, 0)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertIn("state/handoffs/h2.md", lines[0])
        self.assertIn("sess-xyz789", lines[0])

    def test_engine_refusal_propagates_verbatim_and_prints_nothing(self):
        stub = _RecordingClaimHandoffMod(claim_rc=1)
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")

    def test_missing_handoff_path_is_usage_error_no_engine_call(self):
        stub = _RecordingClaimHandoffMod()
        _cli._import_module = lambda: stub

        rc = _cli.main(["claim-handoff"])

        self.assertEqual(rc, 2)
        self.assertEqual(stub.calls, [])


class ClaimHandoffFailureNamingTest(unittest.TestCase):

    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_all_writes_landed_prints_only_the_success_line(self):
        stub = _RecordingClaimHandoffMod(
            claim_rc=0,
            sid="sess-abc123",
            writes={
                "handoff_transition": True,
                "pickup": True,
                "session_goal": True,
                "claimant_identity": True,
            },
        )
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 0)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("WARNING", lines[0])

    def test_a_failed_write_is_named_on_stdout_distinguishable_from_clean_run(self):
        stub = _RecordingClaimHandoffMod(
            claim_rc=0,
            sid="sess-abc123",
            writes={
                "handoff_transition": True,
                "pickup": False,
                "session_goal": True,
                "claimant_identity": True,
            },
        )
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("state/handoffs/h.md", out)
        self.assertIn("WARNING", out)
        self.assertIn("pickup", out)
        self.assertNotIn("session_goal", out)
        self.assertNotIn("claimant_identity", out)

    def test_multiple_failed_writes_are_all_named(self):
        stub = _RecordingClaimHandoffMod(
            claim_rc=0,
            sid="sess-abc123",
            writes={
                "handoff_transition": True,
                "pickup": False,
                "session_goal": False,
                "claimant_identity": True,
            },
        )
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("pickup", out)
        self.assertIn("session_goal", out)

    def test_no_writes_key_degrades_to_plain_success_line(self):
        stub = _RecordingClaimHandoffMod(claim_rc=0, sid="sess-abc123", writes=None)
        _cli._import_module = lambda: stub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = _cli.main(["claim-handoff", "state/handoffs/h.md"])

        self.assertEqual(rc, 0)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
