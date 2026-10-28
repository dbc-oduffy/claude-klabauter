"""The workflow asset a bare relayed ask embeds is the one compose input that grows; its read site caps it."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz, ask_roadmap, emit, prompt_fragments
from coordinator_core.warm.caller_context import resolve_caller_context

_REPO_ROOT = str(Path(ask_compose.__file__).resolve().parents[3])
# Resolved at import: the suite-wide fixture clears CLAUDE_PLUGIN_ROOT before each test.
_PLUGIN_ROOT = resolve_caller_context().plugin_root
_BUDGET = 460800
_PAD_LINE = "void 0;\n"


def _plugin_with_blitz(tmp_path: Path, text: str) -> str:
    wf = tmp_path / "plugin" / "workflows"
    wf.mkdir(parents=True)
    (wf / "plan-blitz.mjs").write_text(text, encoding="utf-8", newline="\n")
    return str(tmp_path / "plugin")


def _grown(text: str, factor: int) -> str:
    """`text` with its byte size multiplied by `factor` through trailing statements the stage keeps."""
    extra = len(text.encode("utf-8")) * (factor - 1)
    return text + _PAD_LINE * (extra // len(_PAD_LINE))


def _compose_bare(plan_blitz_text: str) -> str:
    return ask_compose.compose_ask_script(
        repo_root=_REPO_ROOT,
        prompt="Fix: a short bare ask.",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        script_path="x.mjs",
        ask_source="relayed",
        plan_blitz_text=plan_blitz_text,
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(_PLUGIN_ROOT),
    )


@pytest.fixture
def _production_inputs(monkeypatch):
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS

    assert _PLUGIN_ROOT, "plugin root did not resolve; plan-blitz and roadmap-blitz are unreachable"
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    patched = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else patched(key)
    )
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)


def test_real_plan_blitz_asset_is_under_its_cap_and_bare_ask_under_budget(_production_inputs):
    text = ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT)
    assert len(text.encode("utf-8")) <= ask_compose.STAGE_ASSET_BYTE_CAP
    assert len(_compose_bare(text).encode("utf-8")) <= _BUDGET


def test_asset_grown_to_its_cap_changes_composed_total_by_no_more_than_the_growth(_production_inputs, tmp_path):
    base = ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT)
    base_total = len(_compose_bare(base).encode("utf-8"))
    room = ask_compose.STAGE_ASSET_BYTE_CAP - len(base.encode("utf-8"))
    grown = base + _PAD_LINE * (room // len(_PAD_LINE))
    read = ask_plan_blitz.load_plan_blitz_text(_plugin_with_blitz(tmp_path, grown))
    delta = len(_compose_bare(read).encode("utf-8")) - base_total
    assert 0 <= delta <= room


def test_asset_grown_10x_is_refused_naming_the_asset_and_its_cap(_production_inputs, tmp_path):
    base = ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT)
    read = ask_plan_blitz.load_plan_blitz_text(_plugin_with_blitz(tmp_path, _grown(base, 10)))
    with pytest.raises(ask_compose.AskComposeRefused) as exc:
        _compose_bare(read)
    message = str(exc.value)
    assert "plan-blitz.mjs" in message and str(ask_compose.STAGE_ASSET_BYTE_CAP) in message
