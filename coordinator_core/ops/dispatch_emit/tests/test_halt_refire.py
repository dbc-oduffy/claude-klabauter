"""halt_refire: envelope builder, validator, and the emitted finaliser body."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.win_portability import no_console_creationflags

_CALL = "Workflow({ script: 'x' })"
_RESUME_CALL = "Workflow({ resumeFromRunId: 'r1' })"


def _env(**kw):
    base = dict(call=_CALL, run_id="r1", sizing="s.yaml", halted="review_blocked")
    base.update(kw)
    return hr.refire_next_action(**base)


def test_builder_output_passes_validator():
    assert hr.is_refire_next_action(_env())


def test_each_legacy_kind_maps_into_input_with_fields_intact():
    for legacy_kind, mapped in hr.LEGACY_INPUT_KIND.items():
        legacy = {"kind": legacy_kind, "rulings": [{"id": "J1"}], "x": 1}
        inp = hr.legacy_input(legacy)
        assert inp["kind"] == mapped and inp["legacy_kind"] == legacy_kind
        assert inp["rulings"] == [{"id": "J1"}] and inp["x"] == 1
        assert hr.is_refire_next_action(_env(input=inp))


def test_hand_repair_text_fails_validator():
    assert not hr.is_refire_next_action(_env(detail="Fix the unapplied findings"))
    assert not hr.is_refire_next_action(_env(call=_CALL + " then fix the file"))


def test_two_workflow_calls_fail():
    assert not hr.is_refire_next_action(_env(call=_CALL + "; " + _CALL))


def test_stage_defaults_none_and_passes_through():
    assert _env()["resume"]["stage"] is None
    handle = {"opaque": [1, 2]}
    assert _env(stage=handle)["resume"]["stage"] is handle


def test_stage_js_lands_verbatim():
    js = hr.refire_next_action_js("C", "L", "K", "D", stage_js="_halted.hnd_x")
    assert "stage: _halted.hnd_x" in js
    assert "stage: null" in hr.refire_next_action_js("C", "L", "K", "D")


def test_usage_limit_workflow_resume_passes():
    assert hr.is_refire_next_action(_env(op=hr.OP_WORKFLOW_RESUME, call="resume with resumeFromRunId r1 via Workflow"))
    assert not hr.is_refire_next_action(_env(op=hr.OP_WORKFLOW_RESUME, call="no handle here"))


def test_ruling_input_quoting_fix_the_still_passes():
    inp = {"kind": "rule", "rulings": [{"id": "J1", "ask": "Fix the thing?"}]}
    assert hr.is_refire_next_action(_env(input=inp))


def test_all_owed_prefix_in_js_maps_to_em_discharge():
    js = hr.refire_next_action_js("C", "L", "K", "D")
    assert json.dumps(hr.ALL_OWED_PREFIX) in js and "em-discharge" in js


def test_exempt_and_fanned_sets():
    assert hr.ENVELOPE_EXEMPT_KINDS == ("parked", "no-op")
    assert hr.FANNED_KINDS == ("split", "research")
    assert hr.INPUT_BY_HALT == {"design-review": "touchpoint", "usage_limit": "wait"}


def test_usage_limit_spelling_matches_ask_compose():
    from coordinator_core.ops.dispatch_emit import ask_compose

    assert hr.HALT_USAGE_LIMIT == ask_compose.HALT_USAGE_LIMIT


def test_bad_shapes_fail():
    assert not hr.is_refire_next_action("a string")
    assert not hr.is_refire_next_action(_env(run_id=""))
    assert not hr.is_refire_next_action(_env(input={"kind": "bogus"}))
    assert not hr.is_refire_next_action(_env(op="other"))


def test_js_embeds_each_constant_and_is_idempotent():
    js = hr.refire_next_action_js("C", "L", "K", "D")
    for const in (hr.ENVELOPE_EXEMPT_KINDS, hr.FANNED_KINDS, hr.LEGACY_INPUT_KIND, hr.INPUT_BY_HALT):
        assert json.dumps(list(const) if isinstance(const, tuple) else const) in js
    assert js.index("_legacy.kind === " + json.dumps(hr.REFIRE_KIND)) < js.index("_halted.next_action =")


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_js_runs_under_node(tmp_path):
    js = hr.refire_next_action_js("'CALL'", "_halted.next_action", "_halted.halted", "'why'")
    script = (
        "let _runId='r', _sizingRel='s';\n"
        "let _halted={halted:'usage_limit', next_action:'Everything left is EM or operator work: x'};\n"
        + js
        + "\n_refireNextAction(); const a=JSON.stringify(_halted.next_action); _refireNextAction();"
        "\nconsole.log(JSON.stringify([JSON.parse(a), JSON.stringify(_halted.next_action)===a]));"
        "\nlet p={halted:'parked',next_action:'x'};_halted=p;_refireNextAction();"
        "\nconsole.log(p.next_action);"
    )
    f = tmp_path / "t.js"
    f.write_text(script)
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags()).stdout.splitlines()
    env, idem = json.loads(out[0])
    assert idem and env["op"] == "workflow-resume" and env["input"]["kind"] == "em-discharge"
    assert env["detail"].startswith(hr.ALL_OWED_PREFIX) and out[1] == "x"
