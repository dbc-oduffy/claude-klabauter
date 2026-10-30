"""Repeated constant `schema:` literals are emitted once as `_SCn` thunks and resolve back to the same script."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import agent_body_dedupe as dedupe
from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

_ANCHOR = "// anchor"
_REPO_ROOT = Path(__file__).resolve().parents[4]
# Resolved at import, before the per-test home quarantine hides the real plugin install.
_PLUGIN_ROOT = resolve_plugin_root()
_SCHEMA = '{"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}'


def _compose(monkeypatch, *, hoist: bool) -> str:
    """The production bare ask (installed plan-blitz asset, no stubs), cap lifted so size never refuses."""
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", hoist)
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    return ask_compose.compose_ask_script(
        repo_root=str(_REPO_ROOT), prompt="probe ask", sizing_rel=None, run_id="ask-probe-000000", session_id="probe"
    )


def _single_quoted(script: str) -> str:
    """Every string literal re-expressed single-quoted; JSON-literal protection in `fold_literals` depends on whether a hoisted ref sat inside the object, so the round trip compares the quote-neutral form."""
    out, last = [], 0
    for s, e in prompt_fragments._scan_literals(script):
        out += [script[last:s], "'" + prompt_fragments._single_quoted_content(script, s, e) + "'"]
        last = e
    return "".join(out) + script[last:]


def _src(*schemas: str) -> str:
    calls = "".join(f"await agent('p{i}', {{ schema: {s} }});\n" for i, s in enumerate(schemas))
    return f"{_ANCHOR}\n{calls}"


def test_repeated_constant_schema_becomes_a_thunk_and_round_trips(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    src = _src(_SCHEMA, _SCHEMA, _SCHEMA)
    out = dedupe.dedupe_agent_payloads(src, anchor=_ANCHOR)
    assert out.count(f"const _SC0 = () => ({_SCHEMA});\n") == 1
    assert out.count("schema: _SC0() }") == 3
    assert len(out) < len(src)
    assert dedupe.resolve_schema_refs(out) == src


def test_thunk_precedes_every_use(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    out = dedupe.dedupe_agent_payloads(_src(_SCHEMA, _SCHEMA), anchor=_ANCHOR)
    assert out.index("const _SC0 = ") < out.index("_SC0()")


def test_schema_with_a_free_identifier_stays_inline(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    free = '{"type": "object", "required": ["a", "b"], "properties": {"a": {"enum": someRuntimeList}}, "pad": "xxxxxxxxxxxxxxxx"}'
    src = _src(free, free, free)
    assert dedupe.dedupe_agent_payloads(src, anchor=_ANCHOR) == src


def test_schema_appearing_once_stays_inline(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    src = _src(_SCHEMA)
    assert dedupe.dedupe_agent_payloads(src, anchor=_ANCHOR) == src


def test_disabled_switch_leaves_the_script_byte_identical(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", False)
    src = _src(_SCHEMA, _SCHEMA, _SCHEMA)
    assert dedupe.dedupe_agent_payloads(src, anchor=_ANCHOR) == src


def test_braces_inside_strings_and_comments_do_not_unbalance_the_scan(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    tricky = '{"description": "a } brace and { another", "type": "object", "pad": "xxxxxxxxxxxxxxxxxxxx"}'
    src = _src(tricky, tricky, tricky)
    out = dedupe.dedupe_agent_payloads(src, anchor=_ANCHOR)
    assert out.count("schema: _SC0()") == 3
    assert dedupe.resolve_schema_refs(out) == src


def test_production_ask_resolves_back_to_the_unhoisted_script(monkeypatch):
    plain = _compose(monkeypatch, hoist=False)
    hoisted = _compose(monkeypatch, hoist=True)
    assert "const _SC" not in plain and len(hoisted) < len(plain)
    resolved = prompt_fragments.resolve_fragment_refs(dedupe.resolve_schema_refs(hoisted))
    expected = dedupe.strip_code_indentation(prompt_fragments.fold_literals(plain))
    assert _single_quoted(resolved) == _single_quoted(expected)


def test_indentation_strip_keeps_template_string_and_comment_whitespace():
    src = "  const a = 1;\n    const t = `x\n      y`;\n  /* c\n     d */\n\tfoo();\n"
    assert dedupe.strip_code_indentation(src) == "const a = 1;\nconst t = `x\n      y`;\n/* c\n     d */\nfoo();\n"


def test_every_composed_schema_site_reads_as_a_call_after_its_const(monkeypatch):
    hoisted = _compose(monkeypatch, hoist=True)
    defined = {m.group(1): m.start() for m in re.finditer(r"^const (_SC\d+) = ", hoisted, re.M)}
    uses = [(m.group(1), m.start()) for m in re.finditer(r"\b(_SC\d+)\b(?! =)", hoisted)]
    # The production ask no longer repeats a constant schema literal (the fragment hoist takes the shared
    # text first), so no thunk is required; any `_SCn` that does appear must still be defined before use.
    assert {name for name, _ in uses} <= set(defined)
    for name, at in uses:
        assert hoisted[at + len(name) : at + len(name) + 2] == "()"
        assert defined[name] < at
