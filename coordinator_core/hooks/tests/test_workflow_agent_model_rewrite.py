"""rewrite_agent_models and the Workflow model hook: reroute to sonnet, never deny."""

import pytest

from coordinator_core.hooks import block_workflow_unmodeled_agent as bwua


@pytest.fixture(autouse=True)
def _agents(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    for name, model in (("test-runner", "haiku"), ("worker", "opus"), ("reviewer", "opus")):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\nmodel: {model}\n---\nb\n", encoding="utf-8")
    monkeypatch.setattr(bwua, "_agents_dir", lambda: agents)
    monkeypatch.setattr(bwua, "_rostered_agent_types", lambda: frozenset({"coordinator:reviewer"}))
    monkeypatch.setattr(bwua, "_sentinel_override_active", lambda: False)


def test_no_options_object_gets_one():
    out, r = bwua.rewrite_agent_models("await agent('p')")
    assert out == "await agent('p', { model: 'sonnet' })"
    assert len(r) == 1


def test_options_without_model_gets_model_inserted():
    out, r = bwua.rewrite_agent_models("agent('p', {label:'x'})")
    assert out == "agent('p', { model: 'sonnet', label:'x'})"
    assert "x" in r[0]


def test_trailing_comma_no_options():
    out, _ = bwua.rewrite_agent_models("agent('p',)")
    assert out == "agent('p', { model: 'sonnet' })"


def test_explicit_opus_unrostered_substituted():
    out, r = bwua.rewrite_agent_models('agent("p", {model: "opus"})')
    assert out == "agent(\"p\", {model: 'sonnet'})"
    assert "opus->sonnet" in r[0]


def test_rostered_and_extra_allowlist_keep_opus():
    s = "agent('p', {agentType:'coordinator:reviewer', model:'opus'}); agent('q', {agentType:'coordinator:apm', model:'opus'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_unmodeled_opus_pinned_unrostered_rewritten_rostered_kept():
    out, r = bwua.rewrite_agent_models("agent('p', {agentType:'coordinator:worker'})")
    assert "model: 'sonnet'" in out and len(r) == 1
    s = "agent('p', {agentType:'coordinator:reviewer'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_unmodeled_call_with_haiku_definition_is_untouched():
    s = "agent('p', {agentType:'coordinator:test-runner'})"
    assert bwua.rewrite_agent_models(s) == (s, [])
    res = bwua._handler({"tool_name": "Workflow", "tool_input": {"script": s}})
    assert res == {}


@pytest.mark.parametrize("model", ["'haiku'", "'sonnet'", "pick()", "x ? 'opus' : 'sonnet'"])
def test_other_models_untouched(model):
    s = f"agent('p', {{model: {model}}})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_comments_and_strings_do_not_confuse():
    s = "// agent('c')\nconst t = \"agent('s')\";\nagent('p', {model:'sonnet'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_multiple_calls_all_rewritten():
    out, r = bwua.rewrite_agent_models("agent('a'); agent('b', {model:'opus'})")
    assert out == "agent('a', { model: 'sonnet' }); agent('b', {model:'sonnet'})"
    assert len(r) == 2


def test_handler_inline_rewrites_and_never_denies():
    res = bwua._handler({"tool_name": "Workflow", "tool_input": {"script": "agent('p', {label:'x'})", "name": "n"}})
    hso = res["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "model: 'sonnet'" in hso["updatedInput"]["script"]
    assert hso["updatedInput"]["name"] == "n"
    assert "rerouted to sonnet" in hso["additionalContext"] and "x" in hso["additionalContext"]


def test_handler_scriptpath_becomes_inline_script(tmp_path):
    f = tmp_path / "w.mjs"
    f.write_text("agent('p')", encoding="utf-8")
    hso = bwua._handler({"tool_name": "Workflow", "tool_input": {"scriptPath": str(f)}})["hookSpecificOutput"]
    assert "scriptPath" not in hso["updatedInput"]
    assert hso["updatedInput"]["script"] == "agent('p', { model: 'sonnet' })"


def test_handler_no_change_is_silent():
    assert bwua._handler({"tool_name": "Workflow", "tool_input": {"script": "agent('p',{model:'haiku'})"}}) == {}
