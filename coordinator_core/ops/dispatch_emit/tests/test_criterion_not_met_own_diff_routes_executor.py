"""A criterion_not_met halt over a failing node id in the run's own diff names the executor fix leg."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.review_test_blocker import TEST_BLOCKER
from coordinator_core.win_portability import no_console_creationflags

_SIZING = "state/sizings/x.yaml"
_OWN = "coordinator_core/tests/test_x.py"
_NODE = f"{_OWN}::test_probe"
_WAVE = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 1, "sidecar_path": "s1.md"}]


def _run(falsifier, wave=None, own=(_OWN,)) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    # Same statements the terminal assembly emits at the unclosable-kind site.
    source = (
        f"let _halted = null; const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _planRel = 'docs/plans/p.md';\n"
        f"const _manifest = {{ ledger_item_ids: ['R1'], review_declared_paths: {json.dumps(list(own))} }};\n"
        f"const _reviewWave = {json.dumps(wave or [])}; const _falsifierResult = {json.dumps(falsifier)};\n"
        "const _criterionOpen = true; const _reviseAndRefire = { reason: 'criterion not_met' };\n"
        + ask_compose._refire_next_action_helper_js(_SIZING)
        + "\n"
        "_halted = { halted: 'criterion_not_met', reason: _reviseAndRefire.reason, sizing: _sizingRel, run_id: _runId };\n"
        + ask_compose._criterion_blocker_halt_js(_SIZING)
        + "\nconsole.log(JSON.stringify(_halted));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _falsifier(**kw) -> dict:
    base = {
        "status": "not_met",
        "failing": [_NODE],
        "failing_detail": [{"node_id": _NODE, "message": "AssertionError: probe is None\nmore"}],
    }
    base.update(kw)
    return base


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_own_diff_red_routes_the_executor_with_detail_and_unapplied_findings():
    halted = _run(_falsifier(), _WAVE)

    assert halted["halted"] == ask_compose.HALT_CRITERION_NOT_MET
    assert halted["reason"] == "criterion not_met"
    assert halted["blocker"] == TEST_BLOCKER
    assert halted["failing"][0]["node_id"] == _NODE
    assert halted["detail"] == f"{_NODE}: AssertionError: probe is None"
    assert "executor fix leg against these failing tests" in json.dumps(halted["next_action"])
    assert halted["unapplied_findings"] == 1
    assert [s["slice_id"] for s in halted["slices"]] == ["s1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_foreign_path_red_keeps_the_bare_shape():
    halted = _run(_falsifier(), _WAVE, own=("coordinator_core/other.py",))

    assert "blocker" not in halted and "detail" not in halted
    assert halted["halted"] == ask_compose.HALT_CRITERION_NOT_MET


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_windows_separator_node_id_matches_the_declared_path():
    node = _NODE.replace("/", "\\")
    halted = _run(_falsifier(failing=[node], failing_detail=[]), own=("./" + _OWN,))

    assert halted["blocker"] == TEST_BLOCKER
    assert halted["failing"][0]["node_id"] == node


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_absent_failing_keeps_the_bare_shape():
    halted = _run({"status": "not_met"}, _WAVE)

    assert "blocker" not in halted and "detail" not in halted
