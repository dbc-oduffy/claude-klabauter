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
