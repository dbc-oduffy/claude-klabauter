"""A stage refusal carrying criterion_blocking_drops halts naming each row, its reason and the predecessor sizing to fire before the refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.win_portability import no_console_creationflags

_SIZING = "state/sizings/x.yaml"
_PRED = "state/sizings/pred.yaml"
_NODE = shutil.which("node")
_DROPS = [
    {"id": "B", "reason": "depends_on_plan P1: predecessor not yet coded",
     "predecessor_plan": "docs/plans/p.md", "predecessor_sizing": _PRED},
    {"id": "C", "reason": "withheld by B", "predecessor_plan": "docs/plans/p.md", "predecessor_sizing": _PRED},
]


def _eval(manifest: dict, expr: str):
    if _NODE is None:
        pytest.skip("node is not installed")
    program = (
        f"const _manifest = {json.dumps(manifest)}; const _planRel = 'plan.md'; const _sizingRel = {json.dumps(_SIZING)};\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
    proc = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_reader_lifts_the_reply_key_and_the_halt_manifest_carries_it(monkeypatch):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_owed_handback import _script

    script = _script(monkeypatch)
    assert "_stageBlockingDrops = { rows: r.criterion_blocking_drops" in script
    assert "criterion_blocking_drops: _stageBlockingDrops?.rows ?? []" in script
    assert f"reason: {ask_compose._blocking_drops_reason_js()} || _manifest.error" in script


def test_next_action_chain_reaches_the_blocking_drops_branch_before_the_no_manifest_resume():
    gated = ask_compose._gated_next_action_js(_SIZING)
    blocking = ask_compose._blocking_drops_next_action_js(_SIZING)
    assert blocking in gated
    assert ask_compose._no_manifest_next_action_js(_SIZING) in blocking
    assert blocking.index("Fire the predecessor first") < blocking.index("Then refire")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reason_names_each_row_reason_and_predecessor_sizing():
    reason = _eval({"criterion_blocking_drops": _DROPS}, ask_compose._blocking_drops_reason_js())
    assert f"B (depends_on_plan P1: predecessor not yet coded; fire {_PRED} first)" in reason
    assert f"C (withheld by B; fire {_PRED} first)" in reason


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_next_action_fires_each_distinct_predecessor_once_then_the_refire():
    action = _eval({"criterion_blocking_drops": _DROPS}, ask_compose._gated_next_action_js(_SIZING))["next_action"]
    fire = ask_compose.fire_command(_PRED)
    assert action.count(fire) == 1
    assert action.index(fire) < action.index(ask_compose.fire_command(_SIZING))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unknown_sizing_names_the_predecessor_plan():
    drops = [{**_DROPS[0], "predecessor_sizing": None}]
    action = _eval({"criterion_blocking_drops": drops}, ask_compose._gated_next_action_js(_SIZING))["next_action"]
    assert "the sizing that codes docs/plans/p.md" in action


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_drops_leaves_the_reason_null_and_the_prior_branches_in_force():
    assert _eval({"error": "x"}, ask_compose._blocking_drops_reason_js()) is None
    gated = [{"id": "G1", "owner_repo": "o"}]
    action = _eval({"gated_rows": gated, "owed_plan": "p.md"}, ask_compose._gated_next_action_js(_SIZING))["next_action"]
    assert "clear-gate" in action and "Fire the predecessor first" not in action
