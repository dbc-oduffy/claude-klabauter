"""A raw ask composed at default arguments fits the Workflow runner's byte cap; a failure reports each stage's share."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz, ask_roadmap, emit, prompt_fragments
from coordinator_core.warm.caller_context import resolve_caller_context

_REPO_ROOT = str(Path(ask_compose.__file__).resolve().parents[3])
_CAP = emit._WORKFLOW_SCRIPT_BYTE_CAP
# Resolved at import: the suite-wide fixture clears CLAUDE_PLUGIN_ROOT before each test.
_PLUGIN_ROOT = resolve_caller_context().plugin_root

_STUB_BLITZ = dict(
    wrap_stage=lambda _t, **_k: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
    plan_blitz_text="stub",
)
_STUB_ROADMAP = dict(
    roadmap_blitz_text="export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};",
)


def _assets() -> dict:
    return dict(
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(_PLUGIN_ROOT),
    )


def _compose(**stubs) -> str:
    return ask_compose.compose_ask_script(
        repo_root=_REPO_ROOT,
        prompt="add a writes field to a schema",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        script_path="x.mjs",
        **stubs,
    )


def _bytes(script: str) -> int:
    return len(script.encode("utf-8"))


def _breakdown(assets: dict) -> str:
    """Hoisted bytes of the full raw script, and of the same script with each embedded stage
    stubbed, so each stage's share is the difference. Runs with the cap lifted, to measure only."""
    saved = emit._WORKFLOW_SCRIPT_BYTE_CAP
    emit._WORKFLOW_SCRIPT_BYTE_CAP = 10**9
    try:
        full = _bytes(_compose(**assets))
        no_blitz = _bytes(_compose(**_STUB_BLITZ, roadmap_blitz_text=assets["roadmap_blitz_text"]))
        base = _bytes(_compose(**_STUB_BLITZ, **_STUB_ROADMAP))
    finally:
        emit._WORKFLOW_SCRIPT_BYTE_CAP = saved
    return (
        f"raw ask script is {full} bytes (cap {_CAP}); base {base}, "
        f"plan-blitz stage {full - no_blitz}, roadmap-blitz stage {no_blitz - base}"
    )


@pytest.fixture
def _production_inputs(monkeypatch):
    """Hoisting on, the installed plugin's review roster, and the vitest worker clause a capped box
    adds: the suite-wide fixtures strip all three, and each adds bytes to a real emit."""
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS

    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    patched = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else patched(key)
    )


@pytest.mark.skipif(not _PLUGIN_ROOT, reason="plugin root does not resolve; plan-blitz and roadmap-blitz are unreachable")
def test_raw_ask_script_composes_under_the_runner_cap(_production_inputs):
    assets = _assets()
    try:
        script = _compose(**assets)
    except ask_compose.AskComposeRefused:
        raise AssertionError(_breakdown(assets)) from None
    assert _bytes(script) <= _CAP


_BRIEF_TEXT = "Frame: the plan"
_BRIEF_DELTA_BYTES = 8192  # a short brief may add its own text and framing, never a stage's worth of prompt


def _compose_shape(brief) -> str:
    return ask_compose.compose_ask_script(
        repo_root=_REPO_ROOT,
        prompt="Fix: a short ask.",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        script_path="x.mjs",
        em_brief=brief,
        **_assets(),
    )


@pytest.mark.skipif(not _PLUGIN_ROOT, reason="plugin root does not resolve; plan-blitz and roadmap-blitz are unreachable")
def test_raw_ask_stage_set_does_not_depend_on_a_brief(_production_inputs):
    from coordinator_core.ops.dispatch_emit.ask_brief import EmBrief

    bare = _compose_shape(None)
    briefed = _compose_shape(EmBrief(text=_BRIEF_TEXT, source="inline", context=()))
    assert "async function roadmapBlitz(" not in bare
    assert "async function roadmapBlitz(" not in briefed
    assert _bytes(briefed) - _bytes(bare) < _BRIEF_DELTA_BYTES


def test_cap_refusal_names_the_largest_inlined_component(monkeypatch):
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 1000)
    big = "  // " + "x" * 6000
    with pytest.raises(ask_compose.AskComposeRefused) as exc:
        ask_compose._finish_script([big, "  return {};"], "m.json", "d", ["P"])
    text = str(exc.value)
    assert "1000-byte cap" in text and "largest inlined component // xxx" in text and "(6005 bytes)" in text


_RAW_BUDGET = 460800


@pytest.mark.skipif(not _PLUGIN_ROOT, reason="plugin root does not resolve; plan-blitz and roadmap-blitz are unreachable")
def test_briefed_relayed_asks_differ_only_by_the_context_bytes(_production_inputs):
    from coordinator_core.ops.dispatch_emit import compose_size_probe as probe

    m = probe.measure_paired(_REPO_ROOT)
    assert len(probe.PAIRED_ASK.encode("utf-8")) > 1024
    assert m["bare_total"] <= _RAW_BUDGET, f"bare relayed ask is {m['bare_total']} bytes (budget {_RAW_BUDGET})"
    assert m["right"] <= _RAW_BUDGET
    assert m["delta"] == m["context_bytes"], (
        f"with-context minus bare is {m['delta']} bytes, context is {m['context_bytes']}; "
        f"largest differing component {m['differing_components'][:1]}"
    )


def test_shape_delta_names_the_largest_differing_component():
    from coordinator_core.ops.dispatch_emit import compose_size_probe as probe

    m = probe.shape_delta("same\nsmall\ntail", "same\nsmall!\n" + "x" * 500 + "\ntail")
    assert m["delta"] == 501 + 1 and m["differing_components"][0][1] > 500
    assert probe.shape_delta("a\nb", "a\nb")["differing_components"] == []
