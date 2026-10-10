"""compose_ask_script em_brief: every composed agent() prompt carries _EM_BRIEF; absent a brief nothing changes."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_BRIEF = EmBrief(text="Frame: it's \\ the plan\nline2", source="inline", context=("docs/ctx.md",))


def _compose(wrap=None, **over):
    seen: list[dict] = []

    def _wrap(text, **kw):
        seen.append(kw)
        return _BLITZ_FN, ["Size", "Plan"]

    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=wrap or _wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    script = compose_ask_script(**kw)
    return script, seen


def _agent_prompts(script: str) -> list[str]:
    prompts = []
    for m in re.finditer(r"(?<![\w.])agent\(", script):
        end = script.index("label:", m.end())
        prompts.append(script[m.end():end])
    return prompts


def test_every_composed_agent_prompt_carries_the_brief():
    script, _ = _compose(em_brief=_BRIEF, accept_pending=True)
    prompts = _agent_prompts(script)
    assert len(prompts) >= 9
    assert [p for p in prompts if EM_BRIEF_VAR not in p] == []


def test_every_agent_prompt_on_a_sized_xs_script_carries_the_brief(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script, _ = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_brief=_BRIEF, preamble="pre")
    prompts = _agent_prompts(script)
    assert prompts and [p for p in prompts if EM_BRIEF_VAR not in p] == []


def test_brief_is_declared_once_before_use_with_ask_agent_alias():
    script, seen = _compose(em_brief=_BRIEF)
    assert script.count("const _EM_BRIEF = ") == 1
    assert script.count("const _askAgent = agent;") == 1
    assert script.index("const _EM_BRIEF = ") < script.index("phase('size')")
    assert seen == [{"agent_prefix_var": EM_BRIEF_VAR}]


def test_brief_literal_round_trips_quotes_backslashes_and_newlines():
    script, _ = _compose(em_brief=_BRIEF)
    decl = next(line for line in script.splitlines() if "const _EM_BRIEF = " in line)
    assert "\\\\ the plan\\nline2" in decl and "it\\'s" in decl


def test_without_a_brief_the_script_has_no_brief_surface_and_wrap_takes_no_kwarg():
    script, seen = _compose()
    assert "_EM_BRIEF" not in script and "_askAgent" not in script
    assert seen == [{}]


def test_roadmap_arm_refuses_a_brief(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "roadmap")
    with pytest.raises(AskComposeRefused, match="roadmap"):
        _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_brief=_BRIEF)
