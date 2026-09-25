"""test_archive_stamp_cli.py — argv/stdout unit test for `archive-stamp-cli
claim-handoff`'s AC2 success line and AC2b's per-field failure naming
(P026-C2 for AC2, P026-C9/Track D for AC2b).

Spec: docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-compares-them.md
(P026-C2, P026-C9). AC2: `archive-stamp-cli claim-handoff` prints exactly one
success line on exit 0, naming the handoff path and the claimant session id.
AC2b: a run in which a best-effort write failed is distinguishable from a
clean run by stdout alone, naming by field which write did not land — read
off AC13's `return_result=True` `writes` map.

The `_import_module()` seam is monkeypatched (same idiom as
coordinator/bin/tests/test_archive_stamp_cli_close_handoff.py) so this suite
never requires the engine root to resolve or coordinator_core to be
importable — it asserts ONLY the CLI's own stdout-on-exit-0 behaviour, not
the engine's claim-transition semantics (that is
coordinator_core/ops/tests/test_handoff_transition.py's job).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
archive-stamp-cli is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as the other archive-stamp-cli test files.

Run:
    pytest coordinator/bin/test_archive_stamp_cli.py -v
"""
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
    """Stand-in for coordinator_core.archive_stamp — records the exact
    handoff_path cs_claim_handoff was called with, and stands in for
    resolve_current_session_id so the test controls the sid the CLI prints
    without a real claude-klabauter checkout or a real environment session id.

    `writes` (AC2b/AC13) — the per-write landed/failed map the real
    `cs_claim_handoff(..., return_result=True)` attaches to its result dict
    on a landed claim; `None` here means "omit the key", matching a
    pre-AC13 engine."""

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
        """A non-zero exit from cs_claim_handoff (e.g. a terminal-deployment
        refusal or an unresolvable session id) must not print a success
        line — the engine already wrote its own error to stderr, and this
        row's AC2 only governs the exit-0 case."""
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
    """AC2b (P026-C9): a run with a failed best-effort write is
    distinguishable from a clean run by stdout alone, naming the field."""

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
        """A pre-AC13 stub (no `writes` key at all) must not crash the CLI —
        the map is additive on a landed claim, absence is not a failure."""
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
