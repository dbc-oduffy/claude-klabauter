"""ask_stage refuses a plan still carrying scaffold markers; the ask script carries a test stage."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, scaffold_markers, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL, repo  # noqa: F401


def test_scan_skips_yaml_comment_lines_and_names_first_line():
    text = "# PLACEHOLDER in a template comment\nbody\nedit path/to/file here\n"
    assert scaffold_markers(text) == {"path/to/file": 3}


@pytest.mark.parametrize("marker", ["PLACEHOLDER", "<REPLACE: the title>", "path/to/file"])
def test_unfilled_plan_is_refused_before_staging(repo, marker):  # noqa: F811
    (repo / PLAN_REL).write_text(PLAN + f"\nTODO {marker}\n", encoding="utf-8")
    with pytest.raises(AskStageError, match="^PLAN-SCAFFOLD-UNFILLED: "):
        stage(repo, run_id="r1", plan_rel=PLAN_REL)


def test_filled_plan_still_stages(repo):  # noqa: F811
    (repo / PLAN_REL).write_text(PLAN, encoding="utf-8")
    assert stage(repo, run_id="r1", plan_rel=PLAN_REL).rows


def test_plan_author_prompt_requires_filling_markers(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt="do x", sizing_rel=None, run_id="run-1", session_id=None, **REVIEW_KW
    )
    assert "FILL the scaffold" in script and "<REPLACE:" in script


def test_ask_arm_script_has_a_test_stage_wired_to_the_digest(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt="do x", sizing_rel=None, run_id="run-1", session_id=None, **REVIEW_KW
    )
    assert "_testResult = await agent(" in script
    assert "test:terminal" in script
    assert "_manifest.review_declared_paths.join(', ')" in script
    assert "_gate.arm !== 'xs'" in script
    assert "(_testResult ? _testResult.status" in script
