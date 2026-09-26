"""
coordinator_core.ops.tests.test_handoff_append_session_ledger_two_ceremony

Regression coverage for B15a (docs/plans/2026-09-26-inbox-blitz-part-b-
engine-defects.md): `handoff.append_session_ledger` now refuses only an
IDENTICAL row (same `session_ledger.row_identity(session_id, summary)`),
not merely a second row for the same session — and accepts an optional
free-form `closing_ceremony` param rendered as the summary's trailing
`(<ceremony> close)`.

Coverage:
  (a) a second row for the same sid with a DIFFERENT closing_ceremony appends;
  (b) a second call with IDENTICAL params (same sid, same ceremony, same
      summary) is refused and writes nothing;
  (c) a second no-param call with the same sid and the same summary is
      refused;
  (d) `session_ledger.row_identity` returns equal values for a row and its
      byte-identical copy;
  (e) omitting closing_ceremony leaves the summary unchanged.

FAST TIER: tmpdir + a real `git init` fixture, reusing
`test_handoff_correct_body`'s fixtures — same convention
`test_handoff_append_session_ledger.py` already established.

Spec backlink: coordinator_core/ops/handoff_append_session_ledger.py (B15a)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_correct_body  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_append_session_ledger  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_append_session_ledger import _handler as _append_handler
from coordinator_core.session_ledger import row_identity
from coordinator_core.session_ledger.aggregate_chain_loe import (
    parse_session_ledgers,
    unparseable_ledger_rows,
)

# Reuse the C7 helpers rather than duplicating repo/handoff scaffolding.
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _AUTHOR_SESSION,
    _make_git_repo,
    _seed_claimed_handoff,
)

_APPEND_OP_NAME = "handoff.append_session_ledger"
assert _APPEND_OP_NAME in _REGISTRY, (
    f"import guard failed: {_APPEND_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_append_session_ledger @register_op did not fire"
)

_LEDGER_BODY = (
    "\n## What this covers\n"
    "\n"
    "A realistically-sized body section, standing in for the prose every live "
    "handoff carries ahead of its Session Ledger block, so the anchor context "
    "this op expands to disambiguate never approaches correct_body's own "
    "50%-of-body ratio bound on a fixture this small.\n"
    "\n"
    "## Session Ledger\n"
    "\n"
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)


def _run(coro):
    return asyncio.run(coro)


def _seed(repo: Path, name: str, body: str = _LEDGER_BODY, **kw) -> Path:
    return _seed_claimed_handoff(repo, name, body=body, **kw)


def _call(repo: Path, params: dict, session_id: str = _AUTHOR_SESSION) -> dict:
    """Invoke the handler with `COORDINATOR_SESSION_ID` set for the call's
    duration only — never leaked into a sibling test."""
    prior = os.environ.get("COORDINATOR_SESSION_ID")
    os.environ["COORDINATOR_SESSION_ID"] = session_id
    try:
        return _run(_append_handler(params, repo))
    finally:
        if prior is None:
            os.environ.pop("COORDINATOR_SESSION_ID", None)
        else:
            os.environ["COORDINATOR_SESSION_ID"] = prior


# ---------------------------------------------------------------------------
# (a) A different closing_ceremony is a different row and appends
# ---------------------------------------------------------------------------


class DistinctCeremonyAppendsTest(unittest.TestCase):
    def test_second_ceremony_for_same_session_appends(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-2c-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            first = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "Did the thing",
                    "closing_ceremony": "handoff",
                },
            )
            self.assertEqual(first["exit_code"], 0, first)

            second = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "Did the thing",
                    "closing_ceremony": "workstream-complete",
                },
            )
            self.assertEqual(second["exit_code"], 0, second)
            self.assertTrue(second["applied"])

            text = hpath.read_text(encoding="utf-8")
            self.assertIn("Did the thing (handoff close)", text)
            self.assertIn("Did the thing (workstream-complete close)", text)
            self.assertEqual(unparseable_ledger_rows(text), [])
            self.assertEqual(len(parse_session_ledgers(text)), 2)


# ---------------------------------------------------------------------------
# (b) An identical repeat (same sid, same ceremony, same summary) is refused
# ---------------------------------------------------------------------------


class IdenticalRepeatRefusedTest(unittest.TestCase):
    def test_identical_ceremony_and_summary_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-2c-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            params = {
                "handoff_path": "state/handoffs/2026-08-21-test.md",
                "summary": "Did the thing",
                "closing_ceremony": "handoff",
            }

            first = _call(repo, dict(params))
            self.assertEqual(first["exit_code"], 0, first)
            before = hpath.read_text(encoding="utf-8")

            second = _call(repo, dict(params))
            self.assertEqual(second["exit_code"], 1)
            self.assertFalse(second["applied"])
            self.assertIn("already exists", second["error"])

            after = hpath.read_text(encoding="utf-8")
            self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# (c) A second no-param call with the same sid and same summary is refused
# ---------------------------------------------------------------------------


class NoParamIdenticalSummaryRefusedTest(unittest.TestCase):
    def test_no_ceremony_identical_summary_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-2c-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            params = {
                "handoff_path": "state/handoffs/2026-08-21-test.md",
                "summary": "Did the thing",
            }

            first = _call(repo, dict(params))
            self.assertEqual(first["exit_code"], 0, first)
            before = hpath.read_text(encoding="utf-8")

            second = _call(repo, dict(params))
            self.assertEqual(second["exit_code"], 1)
            self.assertFalse(second["applied"])
            self.assertIn("already exists", second["error"])

            after = hpath.read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_omitted_ceremony_leaves_summary_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-2c-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            result = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "Did the thing",
                },
            )
            self.assertEqual(result["exit_code"], 0, result)
            text = hpath.read_text(encoding="utf-8")
            self.assertIn("Did the thing", text)
            self.assertNotIn("close)", text)


# ---------------------------------------------------------------------------
# (d) row_identity is stable across a byte-identical copy
# ---------------------------------------------------------------------------


class RowIdentityTest(unittest.TestCase):
    def test_row_identity_equal_for_byte_identical_copy(self):
        a = row_identity("AbC123", "Did the thing (handoff close)")
        b = row_identity("AbC123", "Did the thing (handoff close)")
        self.assertEqual(a, b)

    def test_row_identity_case_and_whitespace_insensitive_on_session_id_and_summary(self):
        a = row_identity("AbC123", "  Did the thing  ")
        b = row_identity("abc123", "Did the thing")
        self.assertEqual(a, b)

    def test_row_identity_differs_on_different_summary(self):
        a = row_identity("abc123", "Did the thing (handoff close)")
        b = row_identity("abc123", "Did the thing (workstream-complete close)")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
