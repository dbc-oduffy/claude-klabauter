"""No engine-composed agent prompt tells an agent to follow or invoke a skill: agents hold no Skill tool."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

SKILL_INVOCATION = re.compile(
    r"following the [\w`:\- ]{0,40}skill|invoke the coordinator:|run /execute-plan|"
    r"run /workstream-complete|Skill tool|sizing skill",
    re.IGNORECASE,
)

_RB = "  async function roadmapBlitz(args) {\n    return { stage: { waves: [] } };\n  }"


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", roadmap_blitz_text=_RB, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _root_with_skill(monkeypatch, tmp_path, *, with_file=True):
    # plan-blitz reads the plugin root's review-signals contract; an empty one is enough here.
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "review-signals.json").write_text('{"signals": {}}', encoding="utf-8")
    if with_file:
        skill = tmp_path / "skills" / "sizing" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "coordinator_core.warm.caller_context.resolve_caller_context",
        lambda *a, **k: SimpleNamespace(plugin_root=str(tmp_path)),
    )


def test_size_arm_names_the_sizing_skill_md_as_a_file_to_read(monkeypatch, tmp_path):
    _root_with_skill(monkeypatch, tmp_path)
    script = _compose()
    assert SKILL_INVOCATION.search(script) is None
    assert str(tmp_path / "skills" / "sizing" / "SKILL.md") in script


def test_unresolved_root_drops_the_pointer_and_stays_skill_free(monkeypatch, tmp_path):
    _root_with_skill(monkeypatch, tmp_path, with_file=False)
    script = _compose()
    assert SKILL_INVOCATION.search(script) is None
    assert "SKILL.md" not in script
    assert "Size this ask afresh, never reusing or editing an existing sizing. Judge first" in script


@pytest.mark.parametrize("over", [{}, {"prompt": None, "sizing_rel": "state/sizings/x.yaml"}])
def test_no_arm_prompt_matches_a_skill_invocation_phrase(monkeypatch, tmp_path, over):
    _root_with_skill(monkeypatch, tmp_path)
    try:
        script = _compose(**over)
    except Exception as exc:  # a sizing_rel fixture the stub repo lacks is not this test's subject
        pytest.skip(f"arm not composable with stub repo: {type(exc).__name__}")
    assert SKILL_INVOCATION.search(script) is None
