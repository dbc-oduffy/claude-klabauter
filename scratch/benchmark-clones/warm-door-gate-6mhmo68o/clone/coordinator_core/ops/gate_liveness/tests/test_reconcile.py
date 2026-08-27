"""
Tests for coordinator_core.ops.gate_liveness.reconcile — the `gate_liveness.reconcile`
dry-run-default, precondition-checked `cleared: true` flip writer (C2).

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C2
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.gate_liveness import reconcile
from coordinator_core.ops.gate_liveness.reconcile import (
    _handler,
    reconcile_gate_liveness,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its worktree root.

    `locked_rmw` (F1) resolves its lock dir via `git_common_dir`, which
    requires a real repo — mirrors `test_plan_tasks_mutate._make_git_repo`.
    `gate_liveness.reconcile` is "show_top"-scoped (op_scopes.py), so
    `repo_root` IS the worktree root itself (unlike plan_tasks_mutate's
    "common_dir" scope) — callers below pass `repo`, never `repo / ".git"`.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "gate-liveness-test@claude-klabauter.test")
    _git("config", "user.name", "Gate Liveness Test")
    _git("config", "commit.gpgsign", "false")
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _write_plan(tmp_path: Path, rows_yaml: str, name: str = "plan.md") -> Path:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / name
    plan_path.write_text(
        "# a plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n",
        encoding="utf-8",
    )
    return plan_path


def _write_discharge_memo(
    tmp_path: Path,
    kind: str,
    key_id: str,
    *,
    evidence: str = "inline",
    landed_at: str = "2026-08-21",
    from_id: str = "claude-klabauter-em",
    name: str = "2026-08-21-claude-klabauter-em-discharge.md",
    omit_evidence: bool = False,
) -> Path:
    memo_dir = tmp_path / "cross-repo" / "inbox"
    memo_dir.mkdir(parents=True, exist_ok=True)
    memo_path = memo_dir / name
    lines = [
        "---",
        f"from: {from_id}",
        "to: receiver-em",
        "topic: t",
        "kind: fyi",
        "created: 2026-08-21",
        "discharges:",
        "  closure_key:",
        f"    kind: {kind}",
        f"    id: {key_id}",
    ]
    if not omit_evidence:
        lines.append(f"  evidence: {evidence}")
    lines.append(f"  landed_at: {landed_at}")
    lines.append("---")
    lines.append("")
    lines.append("body text.")
    lines.append("")
    memo_path.write_text("\n".join(lines), encoding="utf-8")
    return memo_path


_ROWS_ONE_GATE = textwrap.dedent(
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


class TestDryRunDefault:
    def test_apply_omitted_proposes_and_writes_nothing(self, tmp_path):
        plan = _write_plan(tmp_path, _ROWS_ONE_GATE)
        _write_discharge_memo(tmp_path, "deliverable", "dlv-foo-abc123")
        before = plan.read_text(encoding="utf-8")

        result = reconcile_gate_liveness(
            "docs/plans/plan.md", False, tmp_path, tmp_path, "2026-08-21"
        )

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert len(result["proposed_flips"]) == 1
        assert result["proposed_flips"][0]["row_id"] == "C1"
        assert plan.read_text(encoding="utf-8") == before  # zero writes

    def test_apply_default_false_via_handler(self, tmp_path):
        plan = _write_plan(tmp_path, _ROWS_ONE_GATE)
        _write_discharge_memo(tmp_path, "deliverable", "dlv-foo-abc123")
        before = plan.read_text(encoding="utf-8")

        result = asyncio.run(
            _handler({"plan_path": "docs/plans/plan.md"}, repo_root=tmp_path)
        )

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert plan.read_text(encoding="utf-8") == before

    def test_no_discharge_yet_proposes_nothing(self, tmp_path):
        plan = _write_plan(tmp_path, _ROWS_ONE_GATE)
        result = reconcile_gate_liveness(
            "docs/plans/plan.md", False, tmp_path, tmp_path, "2026-08-21"
        )
        assert result["exit_code"] == 0
        assert result["proposed_flips"] == []


class TestApplyFlip:
    def test_apply_true_flips_discharged_entry(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        plan = _write_plan(repo, _ROWS_ONE_GATE)
        _write_discharge_memo(repo, "deliverable", "dlv-foo-abc123")

        result = reconcile_gate_liveness(
            "docs/plans/plan.md", True, repo, repo, "2026-08-21"
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True
        assert len(result["flipped"]) == 1

        rows = yaml.safe_load(
            plan.read_text(encoding="utf-8").split("```yaml plan-tasks\n")[1].split("\n```")[0]
        )
        entry = rows[0]["external_gate"][0]
        assert entry["cleared"] is True
        assert "gate_liveness.reconcile 2026-08-21" in entry["closure_evidence"]
        assert "resolver=closure_key" in entry["closure_evidence"]

    def test_apply_true_no_candidates_is_a_noop_write(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        plan = _write_plan(repo, _ROWS_ONE_GATE)  # no matching memo
        before = plan.read_text(encoding="utf-8")

        result = reconcile_gate_liveness(
            "docs/plans/plan.md", True, repo, repo, "2026-08-21"
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True
        assert result["flipped"] == []
        assert plan.read_text(encoding="utf-8") == before

    def test_already_cleared_entry_is_not_a_candidate(self, tmp_path):
        rows = textwrap.dedent(
            """\
            - id: C1
              title: t
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c
                  cleared: true
                  closure_key:
                    kind: deliverable
                    id: dlv-foo-abc123
            """
        )
        plan = _write_plan(tmp_path, rows)
        _write_discharge_memo(tmp_path, "deliverable", "dlv-foo-abc123")
        before = plan.read_text(encoding="utf-8")

        result = reconcile_gate_liveness(
            "docs/plans/plan.md", False, tmp_path, tmp_path, "2026-08-21"
        )
        assert result["proposed_flips"] == []
        assert plan.read_text(encoding="utf-8") == before


class TestNeverFlipsHoldsOrUndetermined:
    def test_no_closure_key_undetermined_never_proposed(self, tmp_path):
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
        repo = _make_git_repo(tmp_path)
        plan = _write_plan(repo, rows)
        result_dry = reconcile_gate_liveness(
            "docs/plans/plan.md", False, repo, repo, "2026-08-21"
        )
        assert result_dry["proposed_flips"] == []

        before = plan.read_text(encoding="utf-8")
        result_apply = reconcile_gate_liveness(
            "docs/plans/plan.md", True, repo, repo, "2026-08-21"
        )
        assert result_apply["flipped"] == []
        assert plan.read_text(encoding="utf-8") == before

    def test_awaiting_discharge_never_proposed(self, tmp_path):
        plan = _write_plan(tmp_path, _ROWS_ONE_GATE)  # closure_key set, no memo
        result = reconcile_gate_liveness(
            "docs/plans/plan.md", False, tmp_path, tmp_path, "2026-08-21"
        )
        assert result["proposed_flips"] == []


class TestCitationRefusal:
    def test_missing_evidence_refuses_whole_batch(self, tmp_path):
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
            - id: C2
              title: t2
              change_kind: code-edit
              surface: s
              external_gate:
                - owner_repo: claude-klabauter
                  condition: c2
                  closure_key:
                    kind: deliverable
                    id: dlv-bar-def456
            """
        )
        repo = _make_git_repo(tmp_path)
        plan = _write_plan(repo, rows)
        # C1's memo is well-formed, C2's is missing `evidence:` entirely.
        _write_discharge_memo(repo, "deliverable", "dlv-foo-abc123", name="m1.md")
        _write_discharge_memo(
            repo,
            "deliverable",
            "dlv-bar-def456",
            name="m2.md",
            omit_evidence=True,
        )
        before = plan.read_text(encoding="utf-8")

        result_dry = reconcile_gate_liveness(
            "docs/plans/plan.md", False, repo, repo, "2026-08-21"
        )
        assert result_dry["exit_code"] == 1
        assert "C2" in result_dry["error"] or "dlv-bar-def456" in result_dry["error"]

        result_apply = reconcile_gate_liveness(
            "docs/plans/plan.md", True, repo, repo, "2026-08-21"
        )
        assert result_apply["exit_code"] == 1
        assert plan.read_text(encoding="utf-8") == before  # zero writes, even C1


class TestConcurrentDriftAborts:
    def test_drift_between_scan_and_lock_aborts_whole_batch(self, tmp_path):
        """AC6: a peer's edit to the row landing after reconcile's pre-lock
        scan but before its locked_rmw acquisition must abort the WHOLE
        batch, never silently clobber the sibling edit."""
        repo = _make_git_repo(tmp_path)
        plan = _write_plan(repo, _ROWS_ONE_GATE)
        _write_discharge_memo(repo, "deliverable", "dlv-foo-abc123")

        real_load_rows = reconcile.load_rows

        def drifting_load_rows(source):
            loaded = real_load_rows(source)
            # Simulate a peer's concurrent edit landing right after this
            # pre-lock read: rewrite the entry's closure_key id on disk.
            drifted = plan.read_text(encoding="utf-8").replace(
                "dlv-foo-abc123", "dlv-foo-drifted"
            )
            plan.write_text(drifted, encoding="utf-8")
            return loaded

        monkeypatch_target = reconcile.load_rows
        try:
            reconcile.load_rows = drifting_load_rows
            result = reconcile_gate_liveness(
                "docs/plans/plan.md", True, repo, repo, "2026-08-21"
            )
        finally:
            reconcile.load_rows = monkeypatch_target

        assert result["exit_code"] == 1
        assert "drift" in result["error"].lower() or "precondition" in result["error"].lower()

        # No partial write: the drifted id survives unclobbered, no `cleared`.
        on_disk = plan.read_text(encoding="utf-8")
        assert "cleared: true" not in on_disk
        assert "dlv-foo-drifted" in on_disk


class TestHandlerErrors:
    def test_missing_plan_path_errors(self, tmp_path):
        result = asyncio.run(_handler({}, repo_root=tmp_path))
        assert result["exit_code"] == 1
        assert "plan_path" in result["error"]

    def test_missing_repo_root_errors(self):
        result = asyncio.run(_handler({"plan_path": "docs/plans/plan.md"}, repo_root=None))
        assert result["exit_code"] == 1
        assert "repo_root" in result["error"]

    def test_nonexistent_plan_path_errors(self, tmp_path):
        result = asyncio.run(
            _handler(
                {"plan_path": "docs/plans/does-not-exist.md"}, repo_root=tmp_path
            )
        )
        assert result["exit_code"] == 1
        assert "plan not found" in result["error"]

    def test_non_bool_apply_errors(self, tmp_path):
        plan = _write_plan(tmp_path, _ROWS_ONE_GATE)
        result = asyncio.run(
            _handler(
                {"plan_path": "docs/plans/plan.md", "apply": "true"}, repo_root=tmp_path
            )
        )
        assert result["exit_code"] == 1
        assert "apply" in result["error"]

    def test_path_escapes_docs_plans_errors(self, tmp_path):
        outside = tmp_path / "elsewhere.md"
        outside.write_text("# not a plan\n", encoding="utf-8")
        result = asyncio.run(
            _handler({"plan_path": "../elsewhere.md"}, repo_root=tmp_path / "docs" / "plans")
        )
        assert result["exit_code"] == 1


class TestZeroSpawns:
    def test_module_imports_no_subprocess(self):
        import coordinator_core.ops.gate_liveness.reconcile as mod

        assert not any(name in ("subprocess", "git") for name in vars(mod))
