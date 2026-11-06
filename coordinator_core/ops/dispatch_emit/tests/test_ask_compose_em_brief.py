"""compose_ask_script em_brief: every composed agent() prompt carries _EM_BRIEF; absent a brief nothing changes."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")

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


# Deliberately unbriefed: the per-wave committer is mechanical (exact message and paths, no task
# judgement), and the blinded falsifier-integrity reviewer row (schema-bearing `work:` call) is
# handed only its brief file so the EM's framing cannot colour its verdict.
_UNBRIEFED = {"commit:wave-", "work:blinded-reviewer"}


def _row_prompt_definition(script: str) -> str:
    start = script.index("const _rowPrompt = ")
    return script[start : script.index("\n", start)]


def _agent_prompts(script: str) -> list[str]:
    """Each agent() prompt expression, with a `_rowPrompt(`/`_groupPrompt(` call resolved to the row-prompt definition it renders."""
    prompts = []
    for m in re.finditer(r"(?<![\w.])agent\(", script):
        end = script.index("label:", m.end())
        label = re.match(r"label: '([^']*)'", script[end:]).group(1)
        if label == "work:" and "schema:" in script[end : script.index("})", end)]:
            label = "work:blinded-reviewer"
        if label not in _UNBRIEFED:
            prompt = script[m.end():end]
            if prompt.lstrip().startswith(("_rowPrompt(", "_groupPrompt(")):
                prompt = _row_prompt_definition(script)
            prompts.append(prompt)
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
    alias = "const _askAgent = agent;"
    iife = script.index("const planBlitz = (() => {")
    assert script[:iife].count(alias) == 1
    assert script[iife:].count(alias) == 1
    assert script.index("const _EM_BRIEF = ") < script.index("phase('size')")
    assert seen == [{"agent_prefix_var": EM_BRIEF_VAR}]


def test_a_brief_script_with_no_blitz_fn_has_one_alias_and_no_rebind(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script, _ = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_brief=_BRIEF, preamble="pre")
    assert script.count("const _askAgent = agent;") == 1
    assert "const planBlitz = (() => {" not in script


def test_a_blitz_script_without_a_brief_carries_no_alias_or_rebind():
    script, _ = _compose()
    assert "const planBlitz = (() => {" in script
    assert "const _askAgent = agent;" not in script


def test_brief_literal_round_trips_quotes_backslashes_and_newlines():
    script, _ = _compose(em_brief=_BRIEF)
    decl = next(line for line in script.splitlines() if "const _EM_BRIEF = " in line)
    assert "\\\\ the plan\\nline2" in decl and "it\\'s" in decl


def test_without_a_brief_the_script_has_no_brief_surface_and_wrap_takes_no_kwarg():
    script, seen = _compose()
    assert "_EM_BRIEF" not in script and "_askAgent" not in script
    assert seen == [{}]


_RB = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"


def test_roadmap_arm_composes_with_a_brief_and_prefixes_the_roadmap_stage(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "roadmap")
    script, _ = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_brief=_BRIEF, roadmap_blitz_text=_RB)
    stage = script[script.index("async function roadmapBlitz(") :]
    assert stage.split("\n", 2)[1] == f"const agent = (p, o) => _askAgent({EM_BRIEF_VAR} + p, o);"
    assert "phase('roadmap-blitz')" in script and "which this script does not embed" not in script
    assert "const _EM_BRIEF = " in script and script.index("const _EM_BRIEF = ") < script.index("phase('roadmap-blitz')")


def test_roadmap_arm_with_a_brief_refuses_when_the_roadmap_stage_cannot_load(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "roadmap")
    with pytest.raises(AskComposeRefused, match="roadmap stage cannot be embedded"):
        _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_brief=_BRIEF)


def test_em_size_rides_every_gate_command_and_is_absent_without_one():
    sized, _ = _compose(em_size="M", accept_pending=True)
    assert 'em_size: "M"' in sized and '"em_size":"M"' in sized
    unsized, _ = _compose(accept_pending=True)
    assert "em_size" not in unsized


def test_em_size_routes_a_roadmap_sizing_into_the_body_that_carries_it(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "roadmap")
    script, _ = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", em_size="M", roadmap_blitz_text=_RB)
    assert 'em_size: "M"' in script and "phase('roadmap-blitz')" in script


_CTX = "pinned citation form [rag:tool(args)] " * 4
_SID = "sid-arrival-check"


@pytest.fixture
def raw_ask_repo(tmp_path, monkeypatch):
    from coordinator_core.ops import baton_pm_turns as PT
    from coordinator_core.session_baton import store

    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "hub" / _SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("split these two jobs please", session_id=_SID)["ok"]
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n")
    return root


def _emit(root, **over):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    params = {"ask": "build it", "ask_source": "relayed", "target_root": root.as_posix(), "session_id": _SID, **over}
    return _dispatch_emit(params, repo_root=root)


def test_a_digest_with_no_loaded_brief_refuses_naming_the_missing_em_context(raw_ask_repo):
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
    from coordinator_core.ops.sizing_intake_contract import em_context_digest

    with pytest.raises(SizingFireRefused, match="em_context .* did not arrive"):
        _emit(raw_ask_repo, em_context_digest=em_context_digest(_CTX))


def test_a_digest_beside_a_brief_without_the_matching_block_refuses(raw_ask_repo):
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
    from coordinator_core.ops.sizing_intake_contract import em_context_digest, with_em_context

    with pytest.raises(SizingFireRefused, match="did not arrive"):
        _emit(raw_ask_repo, ask_brief=with_em_context(None, _CTX + "x"), em_context_digest=em_context_digest(_CTX))


@pytest.mark.parametrize("via_file", [False, True])
def test_a_matching_digest_proceeds_inline_or_from_a_spilled_file(raw_ask_repo, via_file):
    from coordinator_core.ops.sizing_intake_contract import em_context_digest, with_em_context

    brief = with_em_context("prose first", _CTX)
    if via_file:
        (raw_ask_repo / "scratch").mkdir()
        (raw_ask_repo / "scratch" / "b.md").write_text(brief, encoding="utf-8")
        brief_params = {"ask_brief_file": "scratch/b.md"}
    else:
        brief_params = {"ask_brief": brief}
    result = _emit(raw_ask_repo, em_context_digest=em_context_digest(_CTX), **brief_params)
    assert "path" in result, result


def test_a_run_with_no_digest_is_unchecked(raw_ask_repo):
    assert "path" in _emit(raw_ask_repo)