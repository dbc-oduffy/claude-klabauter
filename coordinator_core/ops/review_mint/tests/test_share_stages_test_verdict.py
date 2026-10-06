"""share_stages picks the tests stage by test_verdict_of, falling back to status."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.review_mint.share_stages import ShareStageMissing, assemble_from_share

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "pln-x-123456"
JUDGE = {"status": "met", "observation": "holds"}


def _sc(share, name, **fm):
    share.mkdir(parents=True, exist_ok=True)
    (share / name).write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\nbody\n", encoding="utf-8")


def _share(root, **tests_fm):
    share = root / ".coordinator-local" / "subagent-share" / "sess-1"
    _sc(share, "coordinator-code-reviewer.a1.md", agent_type="coordinator:code-reviewer", target_plan=PLAN)
    _sc(share, "coordinator-test-runner.p1.md", agent_type="coordinator:test-runner",
        target_plan=PLAN, status="open", run_base_sha="abc1234", product_files=2)
    _sc(share, "coordinator-delivery-verifier.d1.md", agent_type="coordinator:delivery-verifier",
        plan=PLAN, verdict="PASS")
    _sc(share, "coordinator-test-runner.t1.md", agent_type="coordinator:test-runner",
        target_plan=PLAN, **tests_fm)


def _assemble(root):
    return assemble_from_share(
        repo_root=root, session_id="sess-1", plan_id=PLAN, plan_stem=None,
        head="HEAD", judge_result=JUDGE,
    )


def test_complete_status_with_test_verdict_is_chosen(tmp_path):
    _share(tmp_path, status="complete", test_verdict="pass", run=124, failed=0)
    res = _assemble(tmp_path)
    assert res["stage_returns"]["tests"]["status"] == "pass"
    assert res["used"]["tests"] == [".coordinator-local/subagent-share/sess-1/coordinator-test-runner.t1.md"]


def test_old_shape_status_pass_still_chosen(tmp_path):
    _share(tmp_path, status="pass", run=4, failed=0)
    assert _assemble(tmp_path)["stage_returns"]["tests"]["status"] == "pass"


def test_complete_without_verdict_raises(tmp_path):
    _share(tmp_path, status="complete")
    with pytest.raises(ShareStageMissing) as exc:
        _assemble(tmp_path)
    assert exc.value.stage == "tests"


def test_errored_verdict_not_selected(tmp_path):
    _share(tmp_path, status="complete", test_verdict="errored")
    with pytest.raises(ShareStageMissing):
        _assemble(tmp_path)
