"""coordinator_core.hooks.tests.test_day_branch_assert_remote_prefix -- the
four `case_b_verdict` outcomes (C3 of docs/plans/2026-09-22-work-branch-
predicates-read-an-origin-prefixed-name.md).

No test of `case_b_verdict` existed before this file. Zero spawn -- it
passes `repo_root` as `tmp_path`, never touched, because `case_b_verdict`
takes `branch` directly and does not shell out.
"""

from __future__ import annotations

from coordinator_core.hooks.day_branch_assert import COMPLIANT, WARN, case_b_verdict


def test_origin_prefixed_work_branch_warns_once_non_escalating(tmp_path):
    result = case_b_verdict(str(tmp_path), "origin/work/m/2026-09-22")
    assert result.outcome == WARN
    assert result.branch == "origin/work/m/2026-09-22"
    assert "origin/work/m/2026-09-22" in result.message
    # Non-escalating: not the banner renderer's output shape.
    assert not result.message.startswith("──")
    assert "day-branch NOT cut" not in result.message
    assert "is not a work/* branch" not in result.message


def test_bare_work_branch_is_compliant(tmp_path):
    result = case_b_verdict(str(tmp_path), "work/m/2026-09-22")
    assert result.outcome == COMPLIANT
    assert result.branch == "work/m/2026-09-22"
    assert result.message == ""


def test_recognized_long_lived_branch_gets_todays_warn(tmp_path):
    result = case_b_verdict(str(tmp_path), "feature/foo")
    assert result.outcome == WARN
    assert result.branch == "feature/foo"
    assert "auto-push is off for feature/foo by doctrine" in result.message


def test_random_branch_gets_the_banner_warn(tmp_path):
    result = case_b_verdict(str(tmp_path), "random")
    assert result.outcome == WARN
    assert result.branch == "random"
    assert result.message.startswith("──")
    assert "random is not a work/* branch" in result.message
