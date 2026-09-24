"""
coordinator_core.ops.tests.test_plan_status_transition_goal_refusal —
coverage for the direct stamp verb's own goal-falsifier gate (P129-C1).

Spec backlink: docs/plans/2026-09-12-the-direct-stamp-verb-refuses-what-
close-out-refuses.md § C1 (AC1-AC7). Mirrors `test_close_out_goal_refusal.py`
against `plan_status_transition._stamp_implemented` (`main(["stamp-
implemented", ...])`) rather than `close_out_and_stamp`, importing that
file's own fixture builders rather than re-deriving them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.execute_plan_assemble.tests.test_close_out_goal_refusal import (
    _PLAN_TEMPLATE,
    _SHIPPED_ROW,
    _asserted_false_block,
    _asserted_pass_block,
    _asserted_true_verdict_fail_block,
    _prime_exit_criterion_block,
    _status_override_block,
)
from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Git repo helpers (mirrors test_close_out_goal_refusal.py's own)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t"], root)
    _run_git(["config", "user.name", "test"], root)


def _head_sha(root: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], root).stdout.strip()


def _land_the_shipping_chunk(root: Path) -> str:
    (root / "fixture.py").write_text("v1", encoding="utf-8")
    _run_git(["add", "fixture.py"], root)
    _run_git(["commit", "-q", "-m", "land the only chunk"], root)
    return _head_sha(root)


def _seed_goal_plan(
    root: Path,
    *,
    goal_frontmatter: str,
    shipping_sha: str,
    status: str = "executing",
    dest_name: str = "plan.md",
    created: str = "2026-08-27",
) -> Path:
    text = _PLAN_TEMPLATE.format(
        status=status,
        goal_frontmatter=goal_frontmatter,
        rows=_SHIPPED_ROW.format(sha=shipping_sha),
    ).replace("created: 2026-08-27", f"created: {created}", 1)
    dest = root / dest_name
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_name], root)
    sizing_rel = "state/sizings/2026-08-27-fixture-sizing.yaml"
    sizing_path = root / sizing_rel
    if not sizing_path.is_file():
        sizing_path.parent.mkdir(parents=True, exist_ok=True)
        sizing_path.write_text("estimate:\n  tshirt: M\n", encoding="utf-8")
        _run_git(["add", sizing_rel], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


def _stamp(plan_path: Path, *extra_args: str) -> int:
    return pst.main(["stamp-implemented", "--plan", str(plan_path), *extra_args])


class TestGoalGateRefusesTheFlip:
    """AC1: a refusing goal gate exits 1, leaves the plan bytes identical,
    and HEAD does not move."""

    def test_falsifier_verdict_not_pass_refuses(self, tmp_path, capsys):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha, exit_criterion_met=_asserted_true_verdict_fail_block()
        )
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)
        before_text = plan_path.read_text(encoding="utf-8")
        before_head = _head_sha(root)

        rc = _stamp(plan_path)

        assert rc == 1
        assert plan_path.read_text(encoding="utf-8") == before_text
        assert _head_sha(root) == before_head
        err = capsys.readouterr().err
        assert "falsifier_verdict_not_pass" in err
        assert "close-out" in err

    def test_exit_criterion_not_asserted_refuses(self, tmp_path, capsys):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha, exit_criterion_met=_asserted_false_block()
        )
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)
        before_text = plan_path.read_text(encoding="utf-8")
        before_head = _head_sha(root)

        rc = _stamp(plan_path)

        assert rc == 1
        assert plan_path.read_text(encoding="utf-8") == before_text
        assert _head_sha(root) == before_head
        err = capsys.readouterr().err
        assert "exit_criterion_not_asserted" in err
        assert "close-out" in err

    def test_falsifier_absent_refuses_with_its_own_next_move(self, tmp_path, capsys):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(baseline_ref=shipping_sha, exit_criterion_met=None)
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)

        rc = _stamp(plan_path)

        assert rc == 1
        err = capsys.readouterr().err
        assert "falsifier_absent" in err
        assert "close-out" in err


class TestGoalGateLetsThePassingPlanThrough:
    """AC2: a plan whose gate passes still stamps."""

    def test_grandfathered_plan_stamps(self, tmp_path, capsys):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        plan_path = _seed_goal_plan(
            root, goal_frontmatter="", shipping_sha=shipping_sha, created="2026-07-01",
        )

        rc = _stamp(plan_path)

        assert rc in (0, 2)
        assert 'status: implemented' in plan_path.read_text(encoding="utf-8")

    def test_passing_falsifier_stamps(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha, exit_criterion_met=_asserted_pass_block()
        )
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)

        rc = _stamp(plan_path)

        assert rc in (0, 2)
        assert 'status: implemented' in plan_path.read_text(encoding="utf-8")

    def test_current_status_override_trio_stamps(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        provisional_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha, exit_criterion_met=None,
        )
        provisional_text = _PLAN_TEMPLATE.format(
            status="executing", goal_frontmatter=provisional_fm,
            rows=_SHIPPED_ROW.format(sha=shipping_sha),
        )
        body_sha = canonical_body_sha(provisional_text)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha,
            exit_criterion_met=None,
            status_override=_status_override_block(body_sha),
        )
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)

        rc = _stamp(plan_path)

        assert rc in (0, 2)
        assert 'status: implemented' in plan_path.read_text(encoding="utf-8")


class TestFrozenStatusStillNoOps:
    """AC3: a frozen-status plan whose goal gate would refuse still no-ops
    cleanly (the gate is never evaluated on that branch)."""

    def test_already_implemented_plan_no_ops(self, tmp_path, capsys):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha, exit_criterion_met=_asserted_false_block()
        )
        plan_path = _seed_goal_plan(
            root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha, status="implemented",
        )

        rc = _stamp(plan_path)

        assert rc == 0
        out = capsys.readouterr().out
        assert "terminal" in out


class TestOverrideReasonDoesNotDischargeTheGoalRefusal:
    """AC4: `--override-reason` never discharges the goal refusal, and is
    itself refused outright on a plan already carrying the status_override_*
    trio."""

    def test_override_reason_does_not_discharge_the_goal_refusal(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(baseline_ref=shipping_sha, exit_criterion_met=None)
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)
        before_text = plan_path.read_text(encoding="utf-8")

        rc = _stamp(plan_path, "--override-reason", "please ship it anyway")

        assert rc == 1
        assert plan_path.read_text(encoding="utf-8") == before_text

    def test_override_reason_refused_when_trio_already_present(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        shipping_sha = _land_the_shipping_chunk(root)
        goal_fm = _prime_exit_criterion_block(
            baseline_ref=shipping_sha,
            exit_criterion_met=_asserted_pass_block(),
            status_override=_status_override_block("deadbeef"),
        )
        plan_path = _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)
        before_text = plan_path.read_text(encoding="utf-8")

        rc = _stamp(plan_path, "--override-reason", "please ship it anyway")

        assert rc == 1
        assert plan_path.read_text(encoding="utf-8") == before_text


class TestNoWorktreeSkipsTheGate:
    """AC5: a flippable plan outside any git worktree stamps without the
    gate ever being evaluated."""

    def test_gate_never_evaluated_outside_a_worktree(self, tmp_path, monkeypatch):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\n"
            "title: t\n"
            "created: 2026-08-27\n"
            "status: executing\n"
            "plan_id: pln-fixture\n"
            "deliverable_id: dlv-fixture\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )

        def _boom(*args, **kwargs):
            raise AssertionError("goal gate must not be evaluated outside a git worktree")

        monkeypatch.setattr(
            "coordinator_core.execute_plan_assemble.close_out_and_stamp."
            "_evaluate_goal_falsifier_gate",
            _boom,
        )

        rc = _stamp(plan_path)

        assert rc == 0
        assert "status: implemented" in plan_path.read_text(encoding="utf-8")
