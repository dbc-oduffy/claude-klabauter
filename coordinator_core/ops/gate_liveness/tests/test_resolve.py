"""
Tests for coordinator_core.ops.gate_liveness.resolve — the `gate_liveness.resolve`
closure_key-join reader (C1).

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C1
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.gate_liveness.resolve import (
    VERDICT_DISCHARGED,
    VERDICT_HOLDS,
    VERDICT_UNDETERMINED,
    _handler,
    resolve_gate_liveness,
)


def _write_plan(tmp_path: Path, name: str, rows_yaml: str) -> Path:
    plan_path = tmp_path / name
    plan_path.write_text(
        "# a plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n",
        encoding="utf-8",
    )
    return plan_path


class TestResolveGateLiveness:
    """AC3 falsification table — one case per shape the census found."""

    def test_no_closure_key_is_undetermined_no_closure_key(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: something must land
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        results = resolve_gate_liveness([plan], tmp_path)
        assert len(results) == 1
        assert results[0]["verdict"] == VERDICT_UNDETERMINED
        assert results[0]["reason"] == "no-closure-key"
        assert results[0]["resolver"] == "closure_key"

    def test_closure_key_no_matching_memo_is_undetermined_awaiting_discharge(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: something must land
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_UNDETERMINED
        assert results[0]["reason"] == "awaiting-discharge"

    def test_matching_discharge_in_inbox_is_discharged(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: something must land
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        memo_dir = tmp_path / "cross-repo" / "inbox"
        memo_dir.mkdir(parents=True)
        memo_path = memo_dir / "2026-08-21-claude-klabauter-em-discharge.md"
        memo_path.write_text(
            textwrap.dedent(
                """\
                ---
                from: claude-klabauter-em
                to: receiver-em
                topic: t
                kind: fyi
                created: 2026-08-21
                discharges:
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
                  evidence: inline
                  landed_at: 2026-08-21
                ---

                body text.
                """
            ),
            encoding="utf-8",
        )
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_DISCHARGED
        assert results[0]["resolver"] == "closure_key"
        assert results[0]["evidence"]["evidence"] == "inline"
        assert results[0]["evidence"]["landed_at"] == "2026-08-21"
        assert str(memo_path) == results[0]["evidence"]["memo_path"]

    def test_matching_discharge_in_archive_is_discharged(self, tmp_path):
        """The boot sweep moves actioned memos — archive must still count."""
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
                  closure_key:
                    kind: memo-thread
                    id: 2026-08-20-ask.md
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        archive_dir = tmp_path / "cross-repo" / "archive" / "2026-08"
        archive_dir.mkdir(parents=True)
        memo_path = archive_dir / "2026-08-21-claude-klabauter-em-discharge.md"
        memo_path.write_text(
            textwrap.dedent(
                """\
                ---
                from: claude-klabauter-em
                to: receiver-em
                topic: t
                kind: fyi
                created: 2026-08-21
                discharges:
                  closure_key:
                    kind: memo-thread
                    id: 2026-08-20-ASK
                  evidence: inline
                  landed_at: 2026-08-21
                ---

                body.
                """
            ),
            encoding="utf-8",
        )
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_DISCHARGED

    def test_wrong_kind_does_not_match(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
                  closure_key:
                    kind: deliverable
                    id: shared-id
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        memo_dir = tmp_path / "cross-repo" / "inbox"
        memo_dir.mkdir(parents=True)
        (memo_dir / "2026-08-21-claude-klabauter-em-discharge.md").write_text(
            textwrap.dedent(
                """\
                ---
                from: claude-klabauter-em
                to: receiver-em
                topic: t
                kind: fyi
                created: 2026-08-21
                discharges:
                  closure_key:
                    kind: memo-thread
                    id: shared-id
                  evidence: inline
                  landed_at: 2026-08-21
                ---

                body.
                """
            ),
            encoding="utf-8",
        )
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_UNDETERMINED
        assert results[0]["reason"] == "awaiting-discharge"

    def test_from_mismatch_owner_repo_routing_check_fails(self, tmp_path):
        """A closure_key collision from an unrelated sender must not discharge."""
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        memo_dir = tmp_path / "cross-repo" / "inbox"
        memo_dir.mkdir(parents=True)
        (memo_dir / "2026-08-21-someone-else-em-discharge.md").write_text(
            textwrap.dedent(
                """\
                ---
                from: someone-else-em
                to: receiver-em
                topic: t
                kind: fyi
                created: 2026-08-21
                discharges:
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
                  evidence: inline
                  landed_at: 2026-08-21
                ---

                body.
                """
            ),
            encoding="utf-8",
        )
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_UNDETERMINED
        assert results[0]["reason"] == "awaiting-discharge"

    def test_status_field_alone_never_discharges(self, tmp_path):
        """Keys on the discharges block ALONE — never on status:."""
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        memo_dir = tmp_path / "cross-repo" / "inbox"
        memo_dir.mkdir(parents=True)
        (memo_dir / "2026-08-21-claude-klabauter-em-status-only.md").write_text(
            textwrap.dedent(
                """\
                ---
                from: claude-klabauter-em
                to: receiver-em
                topic: t
                kind: consult
                status: actioned
                decision: accepted
                realized_by: inline
                created: 2026-08-21
                ---

                No discharges block at all, just an actioned status.
                """
            ),
            encoding="utf-8",
        )
        results = resolve_gate_liveness([plan], tmp_path)
        assert results[0]["verdict"] == VERDICT_UNDETERMINED
        assert results[0]["reason"] == "awaiting-discharge"

    def test_never_holds(self, tmp_path):
        """Neither closure_key shape ever produces `holds` — absence of a
        discharge record is not evidence the blocker is live."""
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
            - id: C2
              title: t2
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c2
                  closure_key:
                    kind: deliverable
                    id: dlv-none-000000
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        results = resolve_gate_liveness([plan], tmp_path)
        assert {r["verdict"] for r in results} <= {VERDICT_UNDETERMINED, VERDICT_DISCHARGED}
        assert VERDICT_HOLDS not in {r["verdict"] for r in results}


class TestHandler:
    def test_missing_plan_path_errors(self, tmp_path):
        result = asyncio.run(_handler({}, repo_root=tmp_path))
        assert result["exit_code"] == 1
        assert "plan_path" in result["error"]

    def test_missing_repo_root_errors(self):
        result = asyncio.run(_handler({"plan_path": "plan.md"}, repo_root=None))
        assert result["exit_code"] == 1
        assert "repo_root" in result["error"]

    def test_nonexistent_plan_path_errors(self, tmp_path):
        result = asyncio.run(
            _handler({"plan_path": "does-not-exist.md"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 1
        assert "does not exist" in result["error"]

    def test_happy_path_returns_verdicts(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
            """
        )
        plan = _write_plan(tmp_path, "plan.md", rows)
        result = asyncio.run(
            _handler({"plan_path": "plan.md"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 0
        assert len(result["verdicts"]) == 1
        assert result["verdicts"][0]["plan"] == str(plan)


class TestZeroSpawns:
    def test_module_imports_no_subprocess(self):
        import sys

        assert "subprocess" not in sys.modules.get(
            "coordinator_core.ops.gate_liveness.resolve"
        ).__dict__
        import coordinator_core.ops.gate_liveness.resolve as mod

        assert not any(
            name in ("subprocess", "git")
            for name in vars(mod)
        )
