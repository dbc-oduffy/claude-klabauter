"""Every composed ask script shape stays within one byte ceiling under the Workflow runner's cap; a failure names the largest component and its bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, compose_size, emit, fan_launcher, prompt_fragments
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_raw_script_under_cap import (
    _PLUGIN_ROOT,
    _STUB_BLITZ,
    _STUB_ROADMAP,
    _assets,
    _bytes,
)

_CAP = emit._WORKFLOW_SCRIPT_BYTE_CAP
_BUDGET = 460800
_BASE_MARGIN = 270000
_SIZING_REL = "state/sizings/2026-10-26-budget-probe.yaml"
_RUN_ID = "ask-20261026T000000-aaaaaa"
_CHILDREN_PREFIX = "const CHILDREN = "
_BARE_ASK = "Fix: a short engine-ask without context is refused as over the Workflow script cap."


@pytest.fixture
def _production_inputs(monkeypatch):
    """Hoisting on, the installed plugin's review roster, and the vitest worker clause a capped box adds."""
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS

    assert _PLUGIN_ROOT, "plugin root did not resolve; plan-blitz and roadmap-blitz are unreachable, so no size pin can run"
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    patched = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else patched(key)
    )


def _write_sizing(root: Path) -> None:
    path = root / _SIZING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"estimate": {"tshirt": "M"}, "route": "plan", "status": "routed", "intent": "budget probe"}),
        encoding="utf-8",
    )


def _write_launcher(root: Path) -> str:
    child = fan_launcher.LaunchChild(
        run_id=_RUN_ID,
        sizing_rel=_SIZING_REL,
        emit_params={"ask": True, "sizing_path": _SIZING_REL, "reserved_run_id": _RUN_ID},
    )
    reply = {"path": f"scratch/warp/{_RUN_ID}.workflow.mjs", "run_id": _RUN_ID, "fire_args": {}}
    return fan_launcher.write_launcher(root, "ask-budget-parent", [child], None, [reply]).read_text(encoding="utf-8")


def _launcher_child_params(written: str) -> dict:
    """The first child's sizing and run id exactly as the written launcher carries them."""
    line = next(ln for ln in written.splitlines() if ln.startswith(_CHILDREN_PREFIX))
    child = json.loads(line[len(_CHILDREN_PREFIX):].rstrip(";"))[0]
    return {"sizing_path": child["sizing_rel"], "reserved_run_id": child["run_id"]}


def _compose(root: Path, shape: str, **stubs) -> str:
    common = dict(repo_root=str(root), session_id=None, script_path="scratch/warp/x.mjs", **stubs)
    if shape == "bare_relayed":
        return ask_compose.compose_ask_script(
            prompt=_BARE_ASK, sizing_rel=None, run_id="run-1", ask_source="relayed", **common
        )
    if shape == "raw":
        return ask_compose.compose_ask_script(
            prompt="add a writes field to a schema", sizing_rel=None, run_id="run-1", **common
        )
    if shape == "refire":
        return ask_compose.compose_ask_script(prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", **common)
    params = _launcher_child_params(_write_launcher(root))
    return ask_compose.compose_ask_script(
        prompt=None,
        sizing_rel=params["sizing_path"],
        run_id=params["reserved_run_id"],
        in_run_commit=True,
        launcher_child=True,
        **common,
    )


def _breakdown(root: Path, shape: str, assets: dict, monkeypatch) -> str:
    """Bytes with the cap lifted, and each embedded stage's share as the difference against a stubbed twin."""
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    full = _bytes(_compose(root, shape, **assets))
    no_blitz = _bytes(_compose(root, shape, **_STUB_BLITZ, roadmap_blitz_text=assets["roadmap_blitz_text"]))
    base = _bytes(_compose(root, shape, **_STUB_BLITZ, **_STUB_ROADMAP))
    parts = {
        "base_script": "x" * base,
        "plan_blitz_stage": "x" * (full - no_blitz),
        "roadmap_blitz_stage": "x" * (no_blitz - base),
    }
    return (
        f"{shape} script is {full} bytes (budget {_BUDGET}); "
        + compose_size.cap_refusal(full, _CAP, compose_size.component_sizes(parts))
    )


def _base_bytes(root: Path, shape: str, monkeypatch) -> int:
    """Bytes with both embedded stage prompts stubbed: the script's own weight over the stage-prompt baseline."""
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    return _bytes(_compose(root, shape, **_STUB_BLITZ, **_STUB_ROADMAP))


@pytest.mark.parametrize("shape", ["bare_relayed", "raw", "refire", "launcher_child"])
def test_composed_script_fits_the_byte_ceiling(shape, tmp_path, monkeypatch, _production_inputs):
    _write_sizing(tmp_path)
    assets = _assets()
    try:
        size = _bytes(_compose(tmp_path, shape, **assets))
    except ask_compose.AskComposeRefused:
        raise AssertionError(_breakdown(tmp_path, shape, assets, monkeypatch)) from None
    assert size <= _BUDGET, _breakdown(tmp_path, shape, assets, monkeypatch)


def test_bare_relayed_script_weight_over_stage_prompt_baseline_is_pinned(tmp_path, monkeypatch, _production_inputs):
    base = _base_bytes(tmp_path, "bare_relayed", monkeypatch)
    assert base <= _BASE_MARGIN, f"bare relayed script minus both stage prompts is {base} bytes (margin {_BASE_MARGIN})"


_SCHEMA = '{"properties": {"alpha": {"type": "string"}, "beta": {"type": "number"}}, "required": ["alpha"], "type": "object"}'


def _hoist(src: str) -> str:
    return prompt_fragments.hoist_repeated_fragments("// anchor\n" + src, anchor="// anchor")


def test_repeated_json_literal_in_code_becomes_one_const(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    src = f"agent(p, {{ schema: {_SCHEMA} }});\nagent(q, {{ schema: {_SCHEMA} }});\n"
    out = _hoist(src)
    assert out.count(_SCHEMA) == 1
    assert prompt_fragments.resolve_fragment_refs(out) == "// anchor\n" + src


def test_json_inside_a_template_literal_is_left_alone(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    src = f"const c = `{_SCHEMA}`;\nconst d = `{_SCHEMA}`;\n"
    assert _hoist(src) == "// anchor\n" + src


def test_json_below_the_floor_is_left_alone(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    tiny = '{"a": 1}'
    src = f"f({tiny});\n" * 60
    assert _hoist(src) == "// anchor\n" + src


def test_repeated_term_pairs_merge_and_resolve_back(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    one = "The first sentence is long enough to be a shared fragment of the script. "
    two = "The second sentence is also long enough to be a shared fragment of it. "
    src = "".join(f"x{k}('{one}{two}' + v{k} + '{one}' + '{two}');\n" for k in range(4))
    out = _hoist(src)
    assert len(out) < len("// anchor\n" + src)
    assert prompt_fragments.resolve_fragment_refs(out) == prompt_fragments.fold_literals("// anchor\n" + src)


def test_a_term_run_ending_in_a_method_call_is_not_merged(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    one = "The first sentence is long enough to be a shared fragment of the script. "
    two = "The second sentence is also long enough to be a shared fragment of it. "
    src = "".join(f"x{k}(v{k} + '{one}' + _w{k}.length + '{two}'.trim());\n" for k in range(4))
    out = _hoist(src)
    assert prompt_fragments.resolve_fragment_refs(out) == prompt_fragments.fold_literals("// anchor\n" + src)
    assert ".trim()" in out and "'.trim()" not in out


def test_launcher_script_fits_the_byte_ceiling(tmp_path):
    size = _bytes(_write_launcher(tmp_path))
    assert size <= _BUDGET, f"launcher script is {size} bytes (budget {_BUDGET})"
