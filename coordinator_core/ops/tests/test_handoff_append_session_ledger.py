"""
coordinator_core.ops.tests.test_handoff_append_session_ledger

Regression coverage for `handoff.append_session_ledger` (AC-5's APPEND half,
state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md).
Reuses `test_handoff_correct_body.py`'s repo/handoff fixtures by import
rather than duplicating them — same convention
`test_handoff_discharge_criteria.py` already established for this op family.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) happy path — no dispatched-agents.txt: row lands with 0d / 0o, XS tier,
      round-trips through `parse_session_ledgers`, and
      `unparseable_ledger_rows` reports zero findings against the result
  (c) dispatch counts read from a real dispatched-agents.txt (agent + opus
      rows), tshirt computed from them
  (d) idempotence — a second append for the SAME session is refused before
      any write, the file untouched
  (e) the appended row lands under `## Session Ledger` only — a sibling
      section (e.g. `## Anti-scope`) is byte-identical before/after
  (f) missing `summary` param refused
  (g) missing `## Session Ledger` heading refused

FAST TIER: tmpdir + a real `git init` fixture (mirrors
`test_handoff_correct_body._make_git_repo` — a real .git dir is required so
`session.core.sessions_dir()`'s filesystem-walk resolver has something to
walk). No engine socket, no network.

Spec backlink: coordinator_core/ops/handoff_append_session_ledger.py
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

_LEDGER_BODY_WITH_ANTI_SCOPE = _LEDGER_BODY + "\n## Anti-scope\n\n- not touched\n"


def _run(coro):
    return asyncio.run(coro)


def _seed(repo: Path, name: str, body: str = _LEDGER_BODY, **kw) -> Path:
    return _seed_claimed_handoff(repo, name, body=body, **kw)


def _write_dispatched_agents(repo: Path, session_id: str, rows: "list[str]") -> None:
    """Plant `<repo>/.git/coordinator-sessions/<sid>/dispatched-agents.txt`
    with tab-delimited `<agentId>\\t<model>\\t<subagent_type>\\t<ts>` rows —
    same shape `hooks.track_dispatched_agents` writes."""
    d = repo / ".git" / "coordinator-sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(row + "\n" for row in rows)
    (d / "dispatched-agents.txt").write_text(text, encoding="utf-8")


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
# (a) Registry assertion
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_op_is_registered(self):
        self.assertIn(_APPEND_OP_NAME, _REGISTRY)


# ---------------------------------------------------------------------------
# (b) Happy path — no dispatched-agents.txt
# ---------------------------------------------------------------------------


class HappyPathTest(unittest.TestCase):
    def test_row_appended_with_zero_dispatches_and_xs_tier(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            result = _call(
                repo,
                {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "Did the thing"},
            )

            self.assertEqual(result["exit_code"], 0, result)
            self.assertTrue(result["applied"])
            self.assertEqual(result["agent_dispatches"], 0)
            self.assertEqual(result["opus_dispatches"], 0)
            self.assertEqual(result["tshirt"], "XS")
            self.assertEqual(result["ledger_session_id"], _AUTHOR_SESSION)

            new_text = hpath.read_text(encoding="utf-8")
            self.assertIn("Did the thing", new_text)
            self.assertEqual(unparseable_ledger_rows(new_text), [])

            recs = parse_session_ledgers(new_text)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["session_id"], _AUTHOR_SESSION[-6:])
            self.assertEqual(recs[0]["agent_dispatches"], "0")
            self.assertEqual(recs[0]["opus_dispatches"], "0")


# ---------------------------------------------------------------------------
# (c) Real dispatch counts
# ---------------------------------------------------------------------------


class DispatchCountTest(unittest.TestCase):
    def test_counts_and_tshirt_reflect_dispatched_agents_file(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md")
            _write_dispatched_agents(
                repo, _AUTHOR_SESSION,
                [
                    "abc123def456\tsonnet\tgeneral-purpose\t1000",
                    "def456abc123\topus\tcode-reviewer\t1001",
                ],
            )

            result = _call(
                repo,
                {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "Ran two agents"},
            )

            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["agent_dispatches"], 2)
            self.assertEqual(result["opus_dispatches"], 1)


# ---------------------------------------------------------------------------
# (d) Idempotence
# ---------------------------------------------------------------------------


class IdempotenceTest(unittest.TestCase):
    def test_second_append_for_same_session_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            first = _call(
                repo,
                {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "First"},
            )
            self.assertEqual(first["exit_code"], 0, first)
            before = hpath.read_text(encoding="utf-8")

            second = _call(
                repo,
                {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "Second"},
            )

            self.assertEqual(second["exit_code"], 1)
            self.assertFalse(second["applied"])
            self.assertIn("already exists", second["error"])
            after = hpath.read_text(encoding="utf-8")
            self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# (e) Only the Session Ledger section changes
# ---------------------------------------------------------------------------


class SiblingSectionUntouchedTest(unittest.TestCase):
    def test_anti_scope_section_untouched(self):
        """The row lands strictly inside the `## Session Ledger` block — the
        `## Anti-scope` section's own bullet is unchanged, whatever else
        `handoff.correct_body` appends at the very end of the body (its own
        stamped Correction Log — a distinct, expected side effect this test
        does not assert against)."""
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md", body=_LEDGER_BODY_WITH_ANTI_SCOPE)

            result = _call(
                repo,
                {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "Kept the boundary"},
            )

            self.assertEqual(result["exit_code"], 0, result)
            after = hpath.read_text(encoding="utf-8")
            self.assertIn("## Anti-scope\n\n- not touched\n", after)
            self.assertIn("Kept the boundary", after.split("## Anti-scope", 1)[0])


# ---------------------------------------------------------------------------
# (f)/(g) Refusals
# ---------------------------------------------------------------------------


class RefusalTest(unittest.TestCase):
    def test_missing_summary_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md")

            result = _call(repo, {"handoff_path": "state/handoffs/2026-08-21-test.md"})

            self.assertEqual(result["exit_code"], 1)
            self.assertIn("summary", result["error"])

    def test_missing_ledger_heading_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md", body="\n## What this covers\n\nprose\n")

            result = _call(
                repo, {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "x"}
            )

            self.assertEqual(result["exit_code"], 1)
            self.assertIn("Session Ledger", result["error"])


# ---------------------------------------------------------------------------
# (h) The session_id override never writes an unmeasured zero
# ---------------------------------------------------------------------------


_FOREIGN_SESSION = "380f3042-4c95-4646-b5cc-a3bea9203689"


class BackfillCountsTest(unittest.TestCase):
    """The override exists for a session that died on another host or with its
    hub, so its `dispatched-agents.txt` is absent BY CONSTRUCTION — the
    absence-is-a-real-zero rule that governs the ordinary path must not reach
    this one. Measured regression (example-store-repo, 2026-09-02): a backfill of
    session ...203689 wrote `XS | 0d / 0o` for a session with 15 sidecars on
    disk, and `aggregate_chain_loe` sums that row as no effort at all."""

    def test_override_with_absent_agents_file_refuses_rather_than_writing_zero(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")
            before = hpath.read_text(encoding="utf-8")

            result = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "Executed all seven chunks",
                    "session_id": _FOREIGN_SESSION,
                },
            )

            self.assertEqual(result["exit_code"], 1, result)
            self.assertFalse(result["applied"])
            self.assertIn("unmeasured", result["error"])
            self.assertIn("agent_dispatches", result["error"])
            self.assertEqual(hpath.read_text(encoding="utf-8"), before)

    def test_supplied_counts_drive_the_row_and_the_tshirt(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            hpath = _seed(repo, "2026-08-21-test.md")

            result = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "Executed all seven chunks",
                    "session_id": _FOREIGN_SESSION,
                    "agent_dispatches": 15,
                    "opus_dispatches": 2,
                },
            )

            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["agent_dispatches"], 15)
            self.assertEqual(result["opus_dispatches"], 2)
            self.assertEqual(result["dispatch_source"], "params")
            self.assertNotEqual(result["tshirt"], "XS")
            self.assertEqual(result["ledger_session_id"], _FOREIGN_SESSION)

            recs = parse_session_ledgers(hpath.read_text(encoding="utf-8"))
            self.assertEqual(recs[0]["session_id"], _FOREIGN_SESSION[-6:])
            self.assertEqual(recs[0]["agent_dispatches"], "15")
            self.assertEqual(recs[0]["opus_dispatches"], "2")

    def test_supplied_counts_win_over_an_on_disk_file(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md")
            _write_dispatched_agents(
                repo, _FOREIGN_SESSION, ["abc123def456	sonnet	general-purpose	1000"],
            )

            result = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "s",
                    "session_id": _FOREIGN_SESSION,
                    "agent_dispatches": 15,
                    "opus_dispatches": 2,
                },
            )

            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["agent_dispatches"], 15)
            self.assertEqual(result["dispatch_source"], "params")

    def test_ordinary_path_still_reads_absence_as_a_real_zero(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md")

            result = _call(
                repo, {"handoff_path": "state/handoffs/2026-08-21-test.md", "summary": "s"}
            )

            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["dispatch_source"], "absent_means_zero")

    def test_half_a_measurement_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
            repo = _make_git_repo(Path(tmp))
            _seed(repo, "2026-08-21-test.md")

            result = _call(
                repo,
                {
                    "handoff_path": "state/handoffs/2026-08-21-test.md",
                    "summary": "s",
                    "session_id": _FOREIGN_SESSION,
                    "agent_dispatches": 15,
                },
            )

            self.assertEqual(result["exit_code"], 1)
            self.assertIn("opus_dispatches", result["error"])

    def test_malformed_counts_are_refused(self):
        cases = [
            ({"agent_dispatches": True, "opus_dispatches": 0}, "integer"),
            ({"agent_dispatches": "15", "opus_dispatches": 2}, "integer"),
            ({"agent_dispatches": -1, "opus_dispatches": 0}, ">= 0"),
            ({"agent_dispatches": 2, "opus_dispatches": 5}, "exceeds"),
        ]
        for counts, fragment in cases:
            with self.subTest(counts=counts):
                with tempfile.TemporaryDirectory(prefix="append-ledger-") as tmp:
                    repo = _make_git_repo(Path(tmp))
                    _seed(repo, "2026-08-21-test.md")

                    params = {
                        "handoff_path": "state/handoffs/2026-08-21-test.md",
                        "summary": "s",
                        "session_id": _FOREIGN_SESSION,
                    }
                    params.update(counts)
                    result = _call(repo, params)

                    self.assertEqual(result["exit_code"], 1, result)
                    self.assertIn(fragment, result["error"])


if __name__ == "__main__":
    unittest.main()
