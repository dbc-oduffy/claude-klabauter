"""A BLOCKED slice whose only unapplied demand is deployed files outside the repo is advisory, never review_blocked."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose import REVIEW_KW, _compose

_ROOT = "C:/repo"
_FOOTPRINT = ["coordinator_core/ops/x.py", "docs/"]
_CACHE_POSIX = "/home/u/.claude/plugins/cache/coordinator/hooks/guard.py"
_CACHE_WIN = "C:\\Users\\u\\.claude\\plugins\\cache\\coordinator\\hooks\\guard.py"
_SETTINGS_BIN = "~/.coordinator-claude-settings/bin/coordinator-invoke"


def _slice(sid: str, findings: int, applied: int, demanded=None, verdict: str = "BLOCKED") -> dict:
    entry = {"slice_id": sid, "verdict": verdict, "findings": findings, "applied": applied, "sidecar_path": f"{sid}.md"}
    if demanded is not None:
        entry["demanded_paths"] = demanded
    return entry


def _run(wave: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        "let _halted = null; const _runId = 'r1'; const _sizingRel = 'state/sizings/x.yaml';\n"
        f"const _reviewWave0 = {json.dumps(wave)}; let _reviewWave = _reviewWave0;\n"
        f"const _manifest = {{ review_declared_paths: {json.dumps(_FOOTPRINT)} }};\n"
        "let _reviewIntegration = { fixes_applied: 0, em_may_think_differently: [], unresolved: [] };\n"
        "const log = () => {};\n"
        + ask_compose._refire_next_action_helper_js(None)
        + "\n"
        + ask_compose._out_of_footprint_demand_js(_ROOT)
        + "\n"
        + ask_compose._review_blocked_halt_js(None)
        + "\n_refireNextAction();\n"
        "console.log(JSON.stringify({ halted: _halted, wave: _reviewWave, advisory: _reviewIntegration.em_may_think_differently }));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("path", [_CACHE_POSIX, _CACHE_WIN, _SETTINGS_BIN])
def test_a_demand_only_for_deployed_files_outside_the_repo_is_advisory(path):
    out = _run([_slice("s1", 2, 0, [path, path])])

    assert out["halted"] is None
    assert out["wave"][0]["out_of_footprint_demand"]
    assert len(out["advisory"]) == 1
    assert "s1" in out["advisory"][0]["line"] and out["advisory"][0]["anchor"] == "s1.md"


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "demanded",
    [
        ["coordinator_core/ops/x.py"],
        ["coordinator_core/ops/other.py"],
        [_CACHE_POSIX, "coordinator_core/ops/x.py"],
        [_ROOT + "/coordinator_core/ops/x.py"],
        [],
        None,
    ],
)
def test_an_in_footprint_mixed_or_unnamed_demand_still_halts_review_blocked(demanded):
    out = _run([_slice("s1", 2, 0, demanded)])

    assert out["halted"]["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert out["halted"]["unapplied_findings"] == 2
    assert "out_of_footprint_demand" not in out["wave"][0]
    assert out["advisory"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_only_the_advisory_slice_is_spared_when_a_peer_slice_blocks_in_footprint():
    out = _run([_slice("ext", 1, 0, [_CACHE_POSIX]), _slice("own", 3, 1, ["docs/a.md"])])

    assert out["halted"]["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert [s["slice_id"] for s in out["halted"]["slices"]] == ["own"]
    assert out["halted"]["unapplied_findings"] == 2


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_non_blocked_slice_naming_external_paths_is_left_untagged():
    out = _run([_slice("w", 2, 0, [_CACHE_POSIX], verdict="WARN")])

    assert out["halted"] is None
    assert "out_of_footprint_demand" not in out["wave"][0]
    assert out["advisory"] == []


def test_the_composed_ask_gives_the_reviewer_the_field_and_tags_before_recover_and_halt():
    slice_schema = {"type": "object", "properties": {"slice_id": {"type": "string"}}}
    schemas = {**REVIEW_KW["review_stage_schemas"], "slice-review-result": slice_schema}
    script = _compose(review_stage_schemas=schemas)

    assert "out_of_footprint_demand" in script
    assert script.index("const _odRoot") < script.index("const _reviewBlockedHalt = ")
    assert script.index("const _odRoot") < script.index("const _reviewRecover = ")
    assert "demanded_paths" in ask_compose._with_demanded_paths(schemas)["slice-review-result"]["properties"]
    assert "demanded_paths" not in slice_schema["properties"]


def test_a_schema_set_without_the_slice_result_is_returned_as_given():
    schemas = {"anchor": {"type": "object"}}

    assert ask_compose._with_demanded_paths(schemas) is schemas
