"""rewrite_agent_models and the Workflow model hook: reroute to sonnet, never deny."""

import hashlib
import json
import random
import statistics
import time
from dataclasses import replace

import pytest

from coordinator_core.hooks import block_workflow_unmodeled_agent as bwua
from pathlib import Path


@pytest.fixture(autouse=True)
def _agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    for name, model in (("test-runner", "haiku"), ("worker", "opus"), ("reviewer", "opus")):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\nmodel: {model}\n---\nb\n", encoding="utf-8")
    monkeypatch.setattr(bwua, "_agents_dir", lambda: agents)
    monkeypatch.setattr(bwua, "_rostered_agent_types", lambda: frozenset({"coordinator:reviewer"}))
    monkeypatch.setattr(bwua, "_sentinel_override_active", lambda: False)


def test_no_options_object_gets_one() -> None:
    out, r = bwua.rewrite_agent_models("await agent('p')")
    assert out == "await agent('p', { model: 'sonnet' })"
    assert len(r) == 1


def test_options_without_model_gets_model_inserted() -> None:
    out, r = bwua.rewrite_agent_models("agent('p', {label:'x'})")
    assert out == "agent('p', { model: 'sonnet', label:'x'})"
    assert "x" in r[0]


def test_trailing_comma_no_options() -> None:
    out, _ = bwua.rewrite_agent_models("agent('p',)")
    assert out == "agent('p', { model: 'sonnet' })"


def test_explicit_opus_unrostered_substituted() -> None:
    out, r = bwua.rewrite_agent_models('agent("p", {model: "opus"})')
    assert out == "agent(\"p\", {model: 'sonnet'})"
    assert "opus->sonnet" in r[0]


def test_rostered_and_extra_allowlist_keep_opus() -> None:
    s = "agent('p', {agentType:'coordinator:reviewer', model:'opus'}); agent('q', {agentType:'coordinator:apm', model:'opus'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_unmodeled_opus_pinned_unrostered_rewritten_rostered_kept() -> None:
    out, r = bwua.rewrite_agent_models("agent('p', {agentType:'coordinator:worker'})")
    assert "model: 'sonnet'" in out and len(r) == 1
    s = "agent('p', {agentType:'coordinator:reviewer'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_unmodeled_call_with_haiku_definition_is_untouched() -> None:
    s = "agent('p', {agentType:'coordinator:test-runner'})"
    assert bwua.rewrite_agent_models(s) == (s, [])
    res = bwua._handler({"tool_name": "Workflow", "tool_input": {"script": s}})
    assert res == {}


@pytest.mark.parametrize("model", ["'haiku'", "'sonnet'", "pick()", "x ? 'opus' : 'sonnet'"])
def test_other_models_untouched(model) -> None:
    s = f"agent('p', {{model: {model}}})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_comments_and_strings_do_not_confuse() -> None:
    s = "// agent('c')\nconst t = \"agent('s')\";\nagent('p', {model:'sonnet'})"
    assert bwua.rewrite_agent_models(s) == (s, [])


def test_multiple_calls_all_rewritten() -> None:
    out, r = bwua.rewrite_agent_models("agent('a'); agent('b', {model:'opus'})")
    assert out == "agent('a', { model: 'sonnet' }); agent('b', {model:'sonnet'})"
    assert len(r) == 2


def test_handler_inline_rewrites_and_never_denies() -> None:
    res = bwua._handler({"tool_name": "Workflow", "tool_input": {"script": "agent('p', {label:'x'})", "name": "n"}})
    hso = res["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "model: 'sonnet'" in hso["updatedInput"]["script"]
    assert hso["updatedInput"]["name"] == "n"
    assert "rerouted to sonnet" in hso["additionalContext"] and "x" in hso["additionalContext"]


def test_handler_scriptpath_becomes_inline_script(tmp_path: Path) -> None:
    f = tmp_path / "w.mjs"
    f.write_text("agent('p')", encoding="utf-8")
    hso = bwua._handler({"tool_name": "Workflow", "tool_input": {"scriptPath": str(f)}})["hookSpecificOutput"]
    assert "scriptPath" not in hso["updatedInput"]
    assert hso["updatedInput"]["script"] == "agent('p', { model: 'sonnet' })"


def test_handler_no_change_is_silent() -> None:
    assert bwua._handler({"tool_name": "Workflow", "tool_input": {"script": "agent('p',{model:'haiku'})"}}) == {}


_VERDICT_FIXTURES = (
    "await agent('p')",
    "agent('p', {label:'x'})",
    "agent('p',)",
    'agent("p", {model: "opus"})',
    "agent('p',{agentType:'coordinator:reviewer',model:'opus'});agent('q',{agentType:'coordinator:apm',model:'opus'})",
    "agent('p', {agentType:'coordinator:worker'})",
    "agent('p', {agentType:'coordinator:test-runner'})",
    *(f"agent('p', {{model: {model}}})" for model in ("'haiku'", "'sonnet'", "pick()", "x ? 'opus' : 'sonnet'")),
    "// agent('c')\nconst t = \"agent('s')\";\nagent('p', {model:'sonnet'})",
    "agent('a'); agent('b', {model:'opus'})",
    "const t = `outer ${{x: `inner ${1}`}.x}`; /* fake agent('x') */ agent(t, {model: null})",
    "const t = 'é'; /* x\n y */ agent('é', {model: undefined}); // café",
    "const t = `escaped \\${agent('fake')} literal\u0024{1}`; agent(t)",
)


def test_existing_guard_verdicts_match_the_previous_scanner_byte_for_byte() -> None:
    verdicts = [bwua.rewrite_agent_models(script) for script in _VERDICT_FIXTURES]
    payload = json.dumps(verdicts, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(payload).hexdigest() == "2aa1aeb3fbdeb8fb9a869fa98d98a3d48c9616d8af37a76233d90d96e5fb1770"


def test_regex_payload_does_not_hide_a_real_agent_after_division() -> None:
    script = r"""const r = /agent\(['"]|[/*]/gi; const q = 8 / n; agent('real');"""
    rewritten, notes = bwua.rewrite_agent_models(script)
    assert rewritten == script.replace("agent('real')", "agent('real', { model: 'sonnet' })")
    assert len(notes) == 1


def test_lexical_masks_match_the_doe_scanner_corpus() -> None:
    # Captured from DoE's scanner including its regex/division heuristic.
    tokens = [
        "//", "/*", "*/", "'", '"', "`", "${", "\\", "\n", "é", "}", "{", " ",
        "agent(", "x", "/", "(", "[", "]", "return ", ")", "typeof ",
    ]
    rng = random.Random(156)
    digest = hashlib.sha256()
    for _ in range(10_000):
        script = "".join(rng.choices(tokens, k=rng.randrange(90)))
        for mask in bwua._scan_masks(script):
            digest.update(len(mask).to_bytes(4, "little"))
            digest.update(mask)
    assert digest.hexdigest() == "31f086e43876dc05f6c76cc75f86b7a6b74bb6b19dad70d923e0ad4dd9d65d89"


def test_guard_scans_a_900_row_emitted_script_under_twenty_ms() -> None:
    from coordinator_core.ops.dispatch_emit.queue_select import ManifestEntry
    from coordinator_core.ops.dispatch_emit.tests import test_grind_compose as fixture

    manifest = replace(
        fixture._fixture_manifest(),
        entries=tuple(
            ManifestEntry(
                row_id=f"row{i}", path=f"state/bug-backlog/row{i}.yaml",
                digest=f"{i:064x}", batch_key="P0",
            )
            for i in range(900)
        ),
    )
    script = fixture._compose(manifest=manifest)
    payload = {"tool_name": "Workflow", "tool_input": {"script": script}}
    bwua._handler(payload)
    timings = []
    for _ in range(5):
        started = time.thread_time_ns()
        for _ in range(5):
            bwua._handler(payload)
        timings.append((time.thread_time_ns() - started) / 5e6)
    assert statistics.median(timings) < 20, timings
