"""The ask script's in-run split: a bundled XL sizing resolves to an auto-firing split, a confirm_split halt, or a refusal halt; never the roadmap arm."""

from __future__ import annotations

import json

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_compose import _XL_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP, run_parent_copy_rel
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_RUN_ID = "run-split-1"


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="ship the exporter and, separately, the dashboard",
        sizing_rel=None,
        run_id=_RUN_ID,
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        in_run_commit=True,
        **REVIEW_KW,
    )


def _split_stage(script: str) -> str:
    start = script.index("if (_halted && _halted.halted === 'split')")
    return script[start : script.index("\n  }", start) + 4]


def test_xl_exit_offers_split_with_slices_and_full_partition():
    props = _XL_SCHEMA["properties"]
    assert props["pick"]["enum"] == ["roadmap", "accept_multi_session", "split"]
    assert set(props["slices"]["items"]["required"]) == {"label", "ask", "rows"}
    script = _script()
    prompt = script[script.index("phase: xl-exit") :]
    prompt = prompt[: prompt.index("recorded")].replace("\\'", "'")
    assert "'split' when its intent or register names two or more independent" in prompt
    assert "Every register row id, a verbatim row included, goes in exactly one slice" in prompt
    assert "'roadmap' stays the pick for one initiative spanning workstreams" in prompt
    assert "['roadmap', 'accept_multi_session', 'split'].includes(_xl.pick)" in script
    assert "_splitSlices = _xl.slices" in script


def test_auto_fire_branch_exits_with_the_xl_exit_agents_fanout_reply():
    script = _script()
    stage = _split_stage(script)
    assert "if (!_confirmSplit && _proposed.length)" in stage
    assert "const _fan = _splitFan;" in stage
    assert "_splitExit = { split: { parent_id: _fan.parent_id, runs: _fan.runs }, fire: _fan.fire ?? [], sizing: _sizingRel, run_id: _runId }" in stage
    assert "await agent(" not in stage
    assert 'label: "split"' not in script and "label: 'split'" not in script
    prompt = script[script.index("phase: xl-exit") :]
    prompt = prompt[: prompt.index("recorded")].replace("\\'", "'")
    assert FANOUT_OP in prompt and "args.confirm_split === true" in prompt
    assert run_parent_copy_rel(_RUN_ID) in prompt
    assert "_splitFan = _xl" in script
    props = _XL_SCHEMA["properties"]
    assert {"parent_id", "runs", "fire", "error"} <= set(props)


def test_confirm_split_halts_with_the_proposed_slices_without_fanout():
    stage = _split_stage(_script())
    assert "args.confirm_split === true" in stage
    assert "_halted = { ..._halted, slices: _proposed, confirm_split: _confirmSplit };" in stage
    guard = stage.index("if (!_confirmSplit && _proposed.length)")
    assert "_fan = _splitFan" in stage[guard:]


def test_partition_refusal_stays_a_split_halt_with_the_refusal_text():
    stage = _split_stage(_script())
    assert "reason: 'split refused: ' + String((_fan && _fan.error) || 'no reply')" in stage
    assert "halted: " not in stage.split("} else {")[-1]


def test_split_never_reaches_roadmap_stage_or_execute():
    script = _script()
    split_at = script.index("if (_halted && _halted.halted === 'split')")
    assert split_at < script.index("if (!_halted && _gate.arm === 'roadmap')")
    after = script[split_at:]
    assert "if (!_viaRoadmap) {\n  if (!_halted) {" in after
    assert "_viaRoadmap = true;" not in _split_stage(script)
    ret = script[script.rindex("  return { arm:") :]
    assert (
        "split: _splitExit ? _splitExit.split : null, fire: _splitExit ? _splitExit.fire : "
        "(_halted && _halted.halted === 'research' && Array.isArray(_halted.fire) ? _halted.fire : "
        "(_commit?.close?.siblings?.released ?? []))"
    ) in ret
    assert "sizing: _sizingRel" in ret and "run_id: _runId" in ret and "halt_commit: _haltCommit" in ret
    assert "if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true))" in script


def test_script_lints_clean_and_declares_the_split_phase():
    script = _script()
    assert not [f for f in run_checks(script) if f.severity == Severity.ERROR]
    assert "'split'" not in script.split("phases: [", 1)[1].split("]", 1)[0]
    json.dumps(_XL_SCHEMA)
