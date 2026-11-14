"""
coordinator_core.authz.tests.test_classification — per-chunk classification pins
for ops landed against docs/plans/2026-08-31-the-hook-category-stops-paying-an-
interpreter-start.md.

Purpose: `test_authz_contract.py` already covers the classification framework
(fail-closed KeyError, drift-guard, registry-size). This module pins the
SPECIFIC entries this plan's chunks add, one test per op, so a later chunk's
edit to `OP_CLASSIFICATION` cannot silently regress an earlier chunk's op
without a named failure pointing at it.

Test convention: pytest. Invoke via
``pytest coordinator_core/authz/tests/test_classification.py -v``
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass, classify


class TestNudgeAutonomousAskuserquestion:

    def test_classifies_compute_only(self) -> None:
        assert classify("hooks.nudge_autonomous_askuserquestion") is OpClass.COMPUTE_ONLY


class TestRecordAskuserquestionAnswer:

    def test_classifies_mutating(self) -> None:
        assert classify("hooks.record_askuserquestion_answer") is OpClass.MUTATING


class TestRepoSetup:

    def test_classifies_mutating(self) -> None:
        assert classify("repo.setup") is OpClass.MUTATING


class TestPlanSetCodeDone:

    def test_classifies_compute_only(self) -> None:
        assert classify("plan.set_code_done") is OpClass.COMPUTE_ONLY

    def test_dispatch_entrypoint_reaches_the_registered_op(self, tmp_path) -> None:
        import asyncio

        from coordinator_core import ipc

        (tmp_path / ".git").mkdir()
        plans = tmp_path / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "p.md").write_text(
            "---\ntitle: fixture\nstatus: draft\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
            "- id: C1\n  title: Ship it\n  body: Do the work.\n  change_kind: code-edit\n"
            "  surface: a.py\n  writes: [a.py]\n  queue_scope: project\n"
            "  disposition: open\n  agent_type: coordinator:executor\n```\n",
            encoding="utf-8",
        )
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "plan.set_code_done",
            "params": {"plans": ["docs/plans/p.md"]},
            "_origin_worktree": str(tmp_path),
        }
        d = asyncio.run(ipc.dispatch_message(msg))
        assert "error" not in d, d
        assert d["result"]["code_complete"] is False
        assert d["result"]["plans"][0]["open_rows"] == ["C1"]
