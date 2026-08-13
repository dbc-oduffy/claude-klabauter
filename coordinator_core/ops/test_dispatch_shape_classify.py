"""Tests for coordinator_core.ops.dispatch_shape_classify.

Golden oracle snapshotted 2026-07-17 against the bash script and its own
co-located test suite — this file re-derives the same fixture cases directly
against `main()`.

Port of: classify-dispatch-shape.sh (coordinator-claude b5a4192c, 2026-07-20)
Oracle: classify-dispatch-shape.test.sh (coordinator-claude a2fe06f8, 2026-07-22)
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_shape_classify import main


def _run(argv, capsys, env_extra=None):
    old_env = {}
    env_extra = env_extra or {}
    for key, val in env_extra.items():
        old_env[key] = os.environ.get(key)
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    try:
        rc = main(argv)
    finally:
        for key, val in old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _init_repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True,
        timeout=10, stdin=subprocess.DEVNULL,
    )


def _write_agents(tmp_path, sid, rows):
    d = tmp_path / ".git" / "coordinator-sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "dispatched-agents.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


_SPINE_ROW = """\
- id: {id}
  title: "chunk {id}"
  change_kind: script-edit
  surface: file-{id}.sh
  deferred: {deferred}
  body: |
    body for {id}
"""


def _spine_row(row_id, deferred):
    return _SPINE_ROW.format(id=row_id, deferred=deferred)


def _write_plan(plan_path, spine_body):
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "# Test Plan\n\nSome plan body text.\n\n## Tasks\n\n"
        "```yaml plan-tasks\n" + spine_body + "```\n\n## Another Section\n\nNothing here.\n",
        encoding="utf-8",
    )


def _write_plan_no_spine(plan_path):
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "# Test Plan\n\n## Tasks\n\nNo machine-parseable spine here — just prose.\n\n"
        "## Another Section\n\nNothing here.\n",
        encoding="utf-8",
    )


def test_no_args_prints_usage_exit_0(capsys):
    rc, out, err = _run([], capsys)
    assert rc == 0
    assert "Usage: classify-dispatch-shape.sh" in err


def test_plan_file_flag_missing_path_exit_0(capsys):
    rc, out, err = _run(["--plan-file"], capsys)
    assert rc == 0
    assert "--plan-file requires a path argument" in err


def test_plan_file_not_found_exit_0(capsys, tmp_path):
    rc, out, err = _run(["--plan-file", str(tmp_path / "nope.md")], capsys)
    assert rc == 0
    assert "plan file not found" in err


def test_a_three_chunks_three_executors_silent(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-aaa111\tsonnet\tgeneral-purpose\t1782100000",
            "agent-bbb222\tsonnet\tgeneral-purpose\t1782100100",
            "agent-ccc333\tsonnet\tgeneral-purpose\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_b_three_chunks_one_executor_offer(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100000",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100100",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" in err
    assert "Was this a serial grind" in err
    assert "pilot-then-expand" in err
    assert "3 non-deferred chunks" in err
    assert "Multi-plan sessions may include" in err


def test_c_no_spine_silent(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        ["agent-solo-001\tsonnet\tgeneral-purpose\t1782100000"],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    _write_plan_no_spine(plan)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_d_deferred_rows_excluded_from_denominator(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(tmp_path, sid, ["agent-solo-001\tsonnet\tgeneral-purpose\t1782100000"])
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "true") + _spine_row("C3", "true")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_d2_deferred_rows_do_not_suppress_offer_when_denominator_gt_1(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100000",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100100",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = (
        _spine_row("C1", "false")
        + _spine_row("C2", "false")
        + _spine_row("C3", "false")
        + _spine_row("C4", "true")
        + _spine_row("C5", "true")
    )
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" in err


def test_f3_reviewer_agents_excluded_from_executor_count(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-exec-001\tsonnet\tgeneral-purpose\t1782100000",
            "agent-rev-001\tunknown\tcoordinator:review-integrator\t1782100100",
            "agent-scout-001\tunknown\tcoordinator:staff-eng\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" in err


def test_f3b_three_executors_plus_reviewers_no_offer(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-exec-001\tsonnet\tgeneral-purpose\t1782100000",
            "agent-exec-002\tsonnet\tgeneral-purpose\t1782100050",
            "agent-exec-003\tsonnet\tgeneral-purpose\t1782100090",
            "agent-rev-001\tunknown\tcoordinator:review-integrator\t1782100100",
            "agent-scout-001\tunknown\tcoordinator:staff-eng\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_f7_zero_executor_agents_no_offer(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-rev-001\tunknown\tcoordinator:review-integrator\t1782100000",
            "agent-rev-002\tunknown\tcoordinator:review-integrator\t1782100100",
            "agent-scout-001\tunknown\tcoordinator:staff-eng\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_g1_multiple_fenced_blocks_malformed_spine_silent(capsys, tmp_path):
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(tmp_path, sid, ["agent-solo-001\tsonnet\tgeneral-purpose\t1782100000"])
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Test Plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: \"chunk C1\"\n  change_kind: script-edit\n"
        "  surface: file-C1.sh\n  deferred: false\n  body: |\n    body for C1\n"
        "```\n\n## Other\n\n```yaml plan-tasks\n"
        "- id: C2\n  title: \"chunk C2\"\n  change_kind: script-edit\n"
        "  surface: file-C2.sh\n  deferred: false\n  body: |\n    body for C2\n"
        "```\n",
        encoding="utf-8",
    )

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_no_git_repo_silent(capsys, tmp_path):
    # No git init at all, and cwd relocated outside any repo — git rev-parse fails
    # both rungs (dirname(plan_file) AND process cwd), classifier exits silently.
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    old_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        rc, out, err = _run(
            ["--plan-file", str(plan)],
            capsys,
            env_extra={"CLAUDE_CODE_SESSION_ID": None, "em_sid": None},
        )
    finally:
        os.chdir(old_cwd)
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_no_agents_file_silent(capsys, tmp_path):
    _init_repo(tmp_path)
    plan = tmp_path / "docs" / "plans" / "test-plan.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    rc, out, err = _run(
        ["--plan-file", str(plan)], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": "no-such-sid"}
    )
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" not in err


def test_slug_resolution_via_script_dir(capsys, tmp_path):
    """A bare slug (no --plan-file) resolves via the docs/plans/ search order,
    using script_dir to derive REPO_ROOT the same way the coordinator-claude trampoline would."""
    _init_repo(tmp_path)
    sid = "test-session-sid-001"
    _write_agents(
        tmp_path,
        sid,
        [
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100000",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100100",
            "agent-solo-001\tsonnet\tgeneral-purpose\t1782100200",
        ],
    )
    plan = tmp_path / "docs" / "plans" / "2026-07-17-my-slug.md"
    spine = _spine_row("C1", "false") + _spine_row("C2", "false") + _spine_row("C3", "false")
    _write_plan(plan, spine)

    old_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        rc, out, err = _run(
            ["2026-07-17-my-slug"], capsys, env_extra={"CLAUDE_CODE_SESSION_ID": sid}
        )
    finally:
        os.chdir(old_cwd)
    assert rc == 0
    assert "DISPATCH SHAPE QUESTION" in err


def test_executor_class_filter_feature_dev_prefix():
    from coordinator_core.ops.dispatch_shape_classify import _is_executor_class

    assert _is_executor_class("general-purpose") is True
    assert _is_executor_class("coordinator:executor") is True
    assert _is_executor_class("feature-dev:code-explorer") is True
    assert _is_executor_class("coordinator:staff-eng") is False
    assert _is_executor_class("coordinator:review-integrator") is False
    assert _is_executor_class("the Staff Engineer") is False
