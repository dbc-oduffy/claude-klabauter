"""test_archive_stamp_cli_stamp_shipped_in.py — argv-parsing regression test
for `archive-stamp-cli stamp-shipped-in` (DR-096 `kind` TypeError fix).

Defect this closes: DR-096 made `kind` a REQUIRED, keyword-only parameter
with no default on `coordinator_core.archive_stamp.stamp_shipped_in` (the
single `shipped_in` write choke point), but this CLI trampoline was never
updated to supply it — EVERY invocation of `stamp-shipped-in` raised
`TypeError: stamp_shipped_in() missing 1 required keyword-only argument:
'kind'`, including the live path reached from
`coordinator/bin/reap-orphaned-in-flight-handoffs.py`.

The `_import_module()` seam is monkeypatched (same idiom as
test_archive_stamp_cli_ship_handoff.py / test_session_claim_cli.py) so this
suite asserts ONLY the argv -> `stamp_shipped_in(...)` call-shape
translation, never the engine behind it (that is
`coordinator_core/test_archive_stamp.py`'s job).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`archive-stamp-cli` is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as the sibling ship-handoff test.

Spec backlink: DR-096 (DoE-claude, 2026-07-26, "kind must be REQUIRED at the
seam, not defaulted silently"), `coordinator_core/archive_stamp.py`'s
`stamp_shipped_in` docstring (kind cross-validation + default-shaped-caller
guidance), `coordinator_core/ops/handoff_stamp.py`'s `_SHIPPED_IN_KIND_ENUM`.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_stamp_shipped_in.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_BIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class _StubStampOutcome:
    """Minimal stand-in shaped like coordinator_core.archive_stamp.StampOutcome
    (a frozen dataclass, not a bare int) — the real return type since chunk
    C0. A stub that returned bare `0` here is exactly why the P0
    (archive-stamp-cli returning the envelope itself as its process exit
    status) went unnoticed by this suite; this stub must be envelope-shaped
    so `sys.exit(main(...))` receiving the envelope instead of `.exit_code`
    would fail these tests.

    Review: code-reviewer — Finding 1 regression test.
    """

    exit_code: int
    applied: bool = False
    replaced: bool = False
    prior_value: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_stamp_shipped_in_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_stamp_shipped_in_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingStampShippedInMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    kwargs stamp_shipped_in was called with, so each test can assert the
    argv -> call-shape translation without a real claude-klabauter checkout."""

    def __init__(self):
        self.calls: list[dict] = []

    def stamp_shipped_in(
        self, handoff_path, *, kind, allow_branch_tip_fallback=False, sha=None,
        force=False, worktree=None,
    ):
        self.calls.append(
            {
                "handoff_path": handoff_path,
                "kind": kind,
                "allow_branch_tip_fallback": allow_branch_tip_fallback,
                "sha": sha,
            }
        )
        return _StubStampOutcome(exit_code=0)


class StampShippedInArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingStampShippedInMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_bare_path_no_flags_defaults_to_scope_derived(self):
        """No --sha, no --kind: must default to kind='scope-derived' and must
        NOT raise TypeError (the regression that matters)."""
        rc = _cli.main(["stamp-shipped-in", "state/handoffs/h.md"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "kind": "scope-derived",
                "allow_branch_tip_fallback": False,
                "sha": None,
            },
        )

    def test_sha_present_defaults_to_ship_commit(self):
        rc = _cli.main(
            ["stamp-shipped-in", "state/handoffs/h.md", "--sha", "f7c81a1d"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "kind": "ship-commit",
                "allow_branch_tip_fallback": False,
                "sha": "f7c81a1d",
            },
        )

    def test_whitespace_only_sha_defaults_to_scope_derived(self):
        """A whitespace-only --sha must default the same as an absent --sha
        (kind='scope-derived'), mirroring stamp_shipped_in's own
        `override = sha.strip() if sha else None` normalization
        (coordinator_core/archive_stamp.py:589). Bare `sha` truthiness would
        pick 'ship-commit' here while the engine strips the sha to '' and
        rejects the call — this is the desync this test guards against."""
        rc = _cli.main(
            ["stamp-shipped-in", "state/handoffs/h.md", "--sha", "   "]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "kind": "scope-derived",
                "allow_branch_tip_fallback": False,
                "sha": "   ",
            },
        )

    def test_explicit_kind_is_honoured(self):
        rc = _cli.main(
            [
                "stamp-shipped-in",
                "state/handoffs/h.md",
                "--sha",
                "f7c81a1d",
                "--kind",
                "successor",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["kind"], "successor")

    def test_explicit_kind_order_independent(self):
        rc = _cli.main(
            [
                "stamp-shipped-in",
                "state/handoffs/h.md",
                "--kind",
                "no-commit",
                "--sha",
                "substantively-shipped-no-commit:2026-07-27",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.calls[-1]["kind"], "no-commit")
        self.assertEqual(
            self.stub.calls[-1]["sha"], "substantively-shipped-no-commit:2026-07-27"
        )

    def test_invalid_kind_is_rejected(self):
        rc = _cli.main(
            ["stamp-shipped-in", "state/handoffs/h.md", "--kind", "bogus-kind"]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_kind_missing_value_is_usage_error(self):
        rc = _cli.main(["stamp-shipped-in", "state/handoffs/h.md", "--kind"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_allow_branch_tip_fallback_still_forwarded(self):
        rc = _cli.main(
            ["stamp-shipped-in", "state/handoffs/h.md", "--allow-branch-tip-fallback"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {
                "handoff_path": "state/handoffs/h.md",
                "kind": "scope-derived",
                "allow_branch_tip_fallback": True,
                "sha": None,
            },
        )

    def test_missing_handoff_path_is_usage_error(self):
        rc = _cli.main(["stamp-shipped-in"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_sha_missing_value_is_usage_error(self):
        rc = _cli.main(["stamp-shipped-in", "state/handoffs/h.md", "--sha"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_returns_exit_code_int_not_the_stamp_outcome_object(self):
        """Regression for the P0: main() must return the StampOutcome
        envelope's `.exit_code` int, never the envelope itself — the CLI is
        invoked as `sys.exit(main(sys.argv[1:]))`, and sys.exit() on a
        non-int/non-None object prints its repr() to stderr and always exits
        1, making success and failure indistinguishable."""
        rc = _cli.main(["stamp-shipped-in", "state/handoffs/h.md"])
        self.assertIsInstance(rc, int)
        self.assertNotIsInstance(rc, _StubStampOutcome)
        self.assertEqual(rc, 0)

    def test_nonzero_exit_code_is_propagated(self):
        """A failing stamp_shipped_in (exit_code=1) must surface as rc == 1,
        not be masked by an always-1 bug nor silently swallowed to 0."""

        class _FailingStub(_RecordingStampShippedInMod):
            def stamp_shipped_in(self, *args, **kwargs):
                super().stamp_shipped_in(*args, **kwargs)
                return _StubStampOutcome(exit_code=1, error="boom")

        self.stub = _FailingStub()
        _cli._import_module = lambda: self.stub
        rc = _cli.main(["stamp-shipped-in", "state/handoffs/h.md"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
