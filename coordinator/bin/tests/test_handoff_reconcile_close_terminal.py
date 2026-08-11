"""test_handoff_reconcile_close_terminal.py — unit test for
`coordinator/bin/handoff-reconcile-close-terminal.py`.

Same idiom as test_handoff_archive_transition.py: monkeypatches the module's
own seams (`_resolve_repo_root`, `cc_invoke.route_mutation`) so this suite
asserts ONLY the CLI's own dispatch and self-verification logic — param
threading, the usage-refusal/refusal/transport exit-code split, and the
on-disk `deployment_state: closed` confirmation — not the engine behind
`handoff.reconcile_close_terminal` (that op has its own test surface under
coordinator_core/).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since the CLI
module has a `.py` extension but is not on `sys.path` as an importable
package member — same load idiom used across coordinator/bin/tests/.

Spec backlink: the two cross-repo memos under `cross-repo/inbox/` reporting
the missing forwarder — 2026-08-05 "reconcile-close-terminal has no
forwarder" and 2026-08-08 "handoff seam coverage directive gaps".

Run:
    pytest coordinator/bin/tests/test_handoff_reconcile_close_terminal.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "handoff_reconcile_close_terminal_test",
        str(_BIN_DIR / "handoff-reconcile-close-terminal.py"),
    )
    spec = importlib.util.spec_from_loader("handoff_reconcile_close_terminal_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingRouteMutation:
    """Stand-in for cc_invoke.route_mutation — records params, returns a
    canned result or raises a canned exception."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result: dict = {}
        self.exc: Exception | None = None

    def __call__(self, op, params, repo_root, legacy_fn):
        self.calls.append({"op": op, "params": params, "repo_root": repo_root})
        if self.exc is not None:
            raise self.exc
        return self.result


class _StubHarness(unittest.TestCase):
    def setUp(self):
        self._orig_route_mutation = _cli.cc_invoke.route_mutation
        self._orig_repo_root = _cli._resolve_repo_root
        self.addCleanup(self._restore)

        self.route_mutation = _RecordingRouteMutation()
        _cli.cc_invoke.route_mutation = self.route_mutation
        _cli._resolve_repo_root = lambda handoff_path: "/fake/repo/root"

        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        self._tmp.close()
        self.handoff_path = self._tmp.name

    def _restore(self):
        _cli.cc_invoke.route_mutation = self._orig_route_mutation
        _cli._resolve_repo_root = self._orig_repo_root
        try:
            os.unlink(self.handoff_path)
        except OSError:
            pass

    def _seed_landed_closed_frontmatter(self, reason: str = "displaced"):
        """The on-disk shape AFTER a real `handoff.reconcile_close_terminal`
        call has landed its close step. `route_mutation` is stubbed
        throughout this harness, so it never performs an actual write — a
        test exercising the CLI's post-write re-read must seed this shape
        itself; a test simulating the op claiming a close it never made
        deliberately withholds it."""
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "deployment_state: closed\n"
                f"closed_reason: {reason}\n"
                "---\n"
                "body\n"
            )


class DispatchTest(_StubHarness):
    def test_success_dispatches_with_reason(self):
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {"closed": True, "archived": True}
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.route_mutation.calls), 1)
        call = self.route_mutation.calls[0]
        self.assertEqual(call["op"], "handoff.reconcile_close_terminal")
        self.assertEqual(call["params"]["handoff_path"], self.handoff_path)
        self.assertEqual(call["params"]["reason"], "displaced")

    def test_missing_reason_is_usage_error(self):
        rc = _cli.cmd_reconcile_close(self.handoff_path, "   ", [])
        self.assertEqual(rc, 2)
        self.assertEqual(self.route_mutation.calls, [])

    def test_exclude_forwarded(self):
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {"closed": True}
        rc = _cli.cmd_reconcile_close(
            self.handoff_path, "displaced", ["state/handoffs/x.md", "state/handoffs/y.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["exclude"],
            ["state/handoffs/x.md", "state/handoffs/y.md"],
        )

    def test_omitting_exclude_adds_no_key(self):
        """Negative-spec: an omitted --exclude must not add a falsy `exclude`
        key — the op's own default is 'no exclusions', and threading an empty
        list would be a silently different call shape."""
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {"closed": True}
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude", self.route_mutation.calls[0]["params"])

    def test_unresolved_repo_root_fails_loud(self):
        _cli._resolve_repo_root = lambda path: None
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])

    def test_op_usage_refusal_surfaces_as_exit_2(self):
        """A `reason` outside the op's own `_CLOSED_REASONS` comes back as an
        envelope exit_code 2. That must reach the shell as 2 (usage), not 1
        (failure) — this discrimination is why the enum is deliberately NOT
        re-declared in the CLI."""
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError(
            "bad reason", {"exit_code": 2}
        )
        rc = _cli.cmd_reconcile_close(self.handoff_path, "nonsense", [])
        self.assertEqual(rc, 2)

    def test_op_refusal_returns_1(self):
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError(
            "refused", {"exit_code": 1}
        )
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)

    def test_transport_failure_returns_1(self):
        """RouteMutationError is a RuntimeError subclass, so the refusal test
        above does not exercise the bare-RuntimeError transport branch."""
        self.route_mutation.exc = RuntimeError("transport down")
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)

    def test_non_dict_result_returns_1(self):
        self.route_mutation.result = "not a dict"  # type: ignore[assignment]
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)


class SelfVerificationTest(_StubHarness):
    def test_op_claims_closed_but_disk_unchanged_fails_loud(self):
        """The core contract this CLI shares with its sibling: a mutation CLI
        that cannot confirm its own write must never exit 0. The op reports
        `closed: True` but the frontmatter never flipped (the harness's
        untouched empty tmp file) — refuse."""
        self.route_mutation.result = {"closed": True, "archived": True}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)
        self.assertIn("the close did not land", stderr.getvalue())

    def test_guard_retained_archival_with_landed_close_returns_0(self):
        """Retention is NOT failure — the op's live-children guard can retain
        the archival move while the close still applies, and the exit code
        promises the CLOSE, never the move. Gating 0 on `archived` would turn
        a correct retention into a reported failure."""
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {
            "closed": True,
            "archived": False,
            "retained": True,
            "retain_reason": "live children found",
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 0)
        self.assertIn("archival retained", stderr.getvalue())

    def test_idempotent_replay_returns_0(self):
        """A second call against an already-closed+archived handoff is the
        op's documented idempotent-replay shape (exit_code 0,
        already_closed/already_archived). The re-read still sees
        `deployment_state: closed`, so this stays 0."""
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {
            "closed": True,
            "already_closed": True,
            "archived": True,
            "already_archived": True,
            "message": "already deployment_state:closed and archived (idempotent replay)",
        }
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 0)

    def test_non_terminal_state_on_disk_fails_loud(self):
        """A landed state that is terminal-but-not-closed (`shipped`) is not
        what this call promised — the re-read asserts the exact enum value,
        not merely 'some terminal'."""
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write("---\ndeployment_state: shipped\n---\nbody\n")
        self.route_mutation.result = {"closed": True}
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 1)

    def test_reread_follows_the_file_into_archive(self):
        """When the archival move landed, the source path no longer exists —
        the re-read must follow the file to its archive/handoffs/YYYY-MM/
        destination rather than reading a hole and reporting a false failure."""
        self._seed_landed_closed_frontmatter()
        archived_copy = self.handoff_path + ".archived"
        Path(archived_copy).write_text(
            Path(self.handoff_path).read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.addCleanup(lambda: os.path.exists(archived_copy) and os.unlink(archived_copy))
        os.unlink(self.handoff_path)

        orig_locate = _cli._locate_after_archive_move
        _cli._locate_after_archive_move = lambda path, root: archived_copy
        self.addCleanup(lambda: setattr(_cli, "_locate_after_archive_move", orig_locate))

        self.route_mutation.result = {"closed": True, "archived": True}
        rc = _cli.cmd_reconcile_close(self.handoff_path, "displaced", [])
        self.assertEqual(rc, 0)


class MainArgvTest(_StubHarness):
    def test_argv_dispatch(self):
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {"closed": True}
        rc = _cli.main([self.handoff_path, "--reason", "displaced"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.route_mutation.calls[0]["params"]["reason"], "displaced")

    def test_argv_exclude_wired_through(self):
        self._seed_landed_closed_frontmatter()
        self.route_mutation.result = {"closed": True}
        rc = _cli.main(
            [self.handoff_path, "--reason", "displaced", "--exclude", "state/handoffs/x.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["exclude"], ["state/handoffs/x.md"]
        )

    def test_no_args_is_usage_error(self):
        rc = _cli.main([])
        self.assertEqual(rc, 2)

    def test_argv_without_reason_is_usage_error(self):
        rc = _cli.main([self.handoff_path])
        self.assertEqual(rc, 2)
        self.assertEqual(self.route_mutation.calls, [])


if __name__ == "__main__":
    unittest.main()
