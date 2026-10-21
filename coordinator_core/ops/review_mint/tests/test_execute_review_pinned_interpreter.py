"""The criterion-judge brief names the target repo's pinned interpreter, not a hard-coded python3."""

import sys

from coordinator_core.ops.review_mint.execute_review import _JUDGE_PREAMBLE, criterion_judge_brief
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _judge_review

_SCHEMAS = {**_STAGE_SCHEMAS, "judge-result": {"type": "object"}}
_FALLBACK = "falling back to `python`"


def _repo(tmp_path, frontmatter):
    (tmp_path / "coordinator.local.md").write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# p\n", encoding="utf-8")
    return plan


def _brief(plan_path):
    prompt, _ = criterion_judge_brief(
        _judge_review(), stage_schemas=_SCHEMAS, plan_path=str(plan_path), run_base_sha="a" * 40
    )
    return prompt


def test_preamble_no_longer_hard_codes_the_interpreter():
    assert _FALLBACK not in _JUDGE_PREAMBLE


def test_pinned_interpreter_reaches_the_judge_brief(tmp_path):
    pin = sys.executable
    plan = _repo(tmp_path, f'targeted_test_python: "{pin}"')
    prompt = _brief(plan)
    assert f"`{pin}`" in prompt
    assert _FALLBACK not in prompt
    assert "bare `python` verbatim" in prompt


def test_no_pin_keeps_the_portable_text(tmp_path):
    plan = _repo(tmp_path, "fast_test_cmd: python3 -m pytest")
    prompt = _brief(plan)
    assert "Run Python as `python3`, falling back to `python`" in prompt


def test_unresolvable_pin_is_a_resolution_defect(tmp_path):
    plan = _repo(tmp_path, "targeted_test_python: no-such-interpreter-xyz")
    prompt = _brief(plan)
    assert "interpreter-resolution-defect:" in prompt
    assert _FALLBACK not in prompt


def test_plan_outside_any_repo_keeps_the_portable_text(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# p\n", encoding="utf-8")
    assert "falling back to `python`" in _brief(plan)
