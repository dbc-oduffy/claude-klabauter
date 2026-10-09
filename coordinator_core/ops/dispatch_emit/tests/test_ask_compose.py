"""compose_ask_script: phase order, size-phase presence, halts before execute, validator cleanliness."""

from __future__ import annotations

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    HALT_REFUSAL,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_RUN_ID = "run-1"


def _stub_wrap(text):
    return _BLITZ_FN, ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id=_RUN_ID,
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _titles(script):
    import re

    block = script.split("phases: [", 1)[1].split("]", 1)[0]
    return re.findall(r"'([^']*)'", block)


def test_phases_follow_the_contract_order_for_a_raw_ask():
    titles = _titles(_compose())
    positions = [titles.index(p) for p in ASK_PHASES]
    assert positions == sorted(positions)


def test_raw_ask_has_a_size_phase_and_a_sized_one_does_not(tmp_path, monkeypatch):
    raw = _compose()
    assert "phase('size')" in raw and "size" in _titles(raw)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    sized = _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    assert "phase('size')" not in sized and "size" not in _titles(sized)
    assert "state/sizings/x.yaml" in sized


def test_exactly_one_of_prompt_or_sizing_is_required():
    with pytest.raises(AskComposeRefused):
        _compose(prompt=None, sizing_rel=None)
    with pytest.raises(AskComposeRefused):
        _compose(sizing_rel="state/sizings/x.yaml")


def test_each_halt_assigns_before_the_guarded_stage_and_execute():
    script = _compose()
    halt_at = script.index("_gate.halt || !_gate.arm")
    assert halt_at < script.index("phase('stage')") < script.index("phase('execute')")
    assert script.index("_halted = { halted:", halt_at) < script.index("phase('stage')")
    stage_guard = script.rindex("if (!_halted) {", 0, script.index("phase('stage')"))
    exec_guard = script.rindex("if (!_halted) {", 0, script.index("phase('execute')"))
    assert halt_at < stage_guard < exec_guard
    assert "return { halted:" not in script
    assert "_halted = { halted: 'no-op'," in script
    assert "..._gate.halt" in script
    assert f"'{HALT_REFUSAL}'" in script


def test_script_carries_manifest_marker_and_both_verbs():
    script = _compose()
    assert f"\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{_RUN_ID}/manifest.json\n" in script
    assert OP_ASK_GATE in script and OP_ASK_STAGE in script


def test_script_validates_with_zero_errors():
    findings = run_checks(_compose())
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_no_fire_substring_and_no_clock_or_spawn_globals():
    script = _compose()
    assert "--fire" not in script
    assert "Date.now" not in script and "Math.random" not in script


def test_review_wave_declares_paths_from_the_manifest():
    script = _compose()
    assert "_manifest.review_declared_paths" in script
    assert script.index("phase('execute')") < script.index("phase('Review prep')")


def test_m_plus_stage_is_embedded_only_when_the_arm_can_be_m_plus(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    assert "async function planBlitz(" not in _compose(prompt=None, sizing_rel="s.yaml")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    assert "async function planBlitz(" in _compose(prompt=None, sizing_rel="s.yaml")
    assert "async function planBlitz(" in _compose()


def test_blitz_phases_merge_into_meta_after_plan():
    titles = _titles(_compose())
    assert titles.index("plan") < titles.index("Size") < titles.index("stage")


def test_wrapped_stage_without_the_entry_function_is_refused():
    with pytest.raises(AskComposeRefused):
        _compose(wrap_stage=lambda t: ("function other() {}", []))


def test_prompt_text_is_escaped_into_the_script():
    script = _compose(prompt="back`tick ${x} \\ 'q'")
    assert "\\\\ \\'q\\'" in script
    assert not any(f.severity is Severity.ERROR for f in run_checks(script))


def test_size_prompt_walks_the_agent_to_a_gate_passing_sizing():
    script = _compose()
    size_line = next(ln for ln in script.splitlines() if "const _sized" in ln)
    for needle in (
        "sizing-assemble",
        "coordinator-doc-new --type sizing-object",
        "--exit-criterion",
        "--interaction-mode",
        "from `draft` to `sized`",
        "`exit_criterion.accepted` null",
        "`writes`",
    ):
        assert needle in size_line, needle


def test_size_agent_must_return_a_footprint():
    # The C9 live run's size agent returned writes [] for an ask naming its
    # one file, and the XS gate refused; the schema now requires it.
    from coordinator_core.ops.dispatch_emit import ask_compose

    assert "writes" in ask_compose._SIZE_SCHEMA["required"]


def test_return_carries_the_plan_routes_terminal_commit_next_action():
    script = _compose(script_path="scratch/warp/ask.workflow.mjs", session_id="d7b9dc1a-1455-43e8-922f-87e734b5634e")
    ret = script[script.rindex("  return { arm:"):]
    assert "next_action: { kind: 'terminal_commit', op: 'dispatch.terminal_commit'" in ret
    assert 'script_path: "scratch/warp/ask.workflow.mjs"' in ret
    assert 'session_id: "d7b9dc1a-1455-43e8-922f-87e734b5634e"' in ret
    assert f"integration_stem: '{_RUN_ID}.review-wave-bookkeeping'" in ret
    assert "plan_id: (_manifest.plan_id ?? null)" in ret
    for field in ("wave_sidecar_paths:", "prep:", "delivery:", "incomplete_chunks:"):
        assert field in ret, field


def test_size_phase_schema_and_prompt_require_gated_rows():
    script = _compose()
    size_stage = script.split("const _sized = await", 1)[1].split("_sizingRel = _sized", 1)[0]
    assert '"gated"' in size_stage and '"owner_repo"' in size_stage
    assert "`gated`" in size_stage
    assert "gated: _gated" in script


def test_degraded_host_ask_review_stages_emit_no_coordinator_review_type_and_keep_every_stage():
    import re

    def types(script):
        return set(re.findall(r"agentType:\s*['\"]coordinator:([^'\"]+)", script))

    review_types = {"code-reviewer", "integrator", "review-prep"}
    normal = _compose()
    degraded = _compose(agent_type_host="host")
    assert review_types <= types(normal)
    assert not review_types & types(degraded)
    assert _titles(degraded) == _titles(normal)


_JUDGE_FRAGMENT = {
    **REVIEW_KW["review_roster_fragment"],
    "execute_review": {
        "stages": [
            *REVIEW_KW["review_roster_fragment"]["execute_review"]["stages"],
            {
                "kind": "judge",
                "agents": [
                    {"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge"}
                ],
            },
        ]
    },
}
_JUDGE_KW = {
    "review_roster_fragment": _JUDGE_FRAGMENT,
    "review_stage_schemas": {**REVIEW_KW["review_stage_schemas"], "judge": {"type": "object"}},
}


def test_a_roster_judge_runs_in_the_test_phase_and_feeds_the_criterion_digest():
    script = _compose(**_JUDGE_KW)
    assert "agentType: 'coordinator:criterion-judge'" in script
    assert "Criterion judge" in _titles(script)
    assert "[_testResult, _falsifierResult] = await parallel([" in script
    assert "let _falsifierResult = null;" in script
    assert "(_planRel ?? _sizingRel)" in script
    assert "PLAN_PATH_SLOT_X" not in script
    assert "criterion leg threw" in script
    assert "_falsifierResult.differs_from_baseline" in script


def test_the_judge_leg_runs_on_the_xs_arm_where_the_scoped_test_does_not():
    script = _compose(**_JUDGE_KW)
    assert "() => (_gate.arm !== 'xs' && (_manifest.review_declared_paths ?? []).length) ? agent(" in script


def test_a_roster_without_a_judge_leaves_the_criterion_unrun():
    script = _compose()
    assert "_falsifierResult" not in script
    assert "Criterion judge" not in _titles(script)


def test_the_judge_script_validates_with_zero_errors():
    findings = run_checks(_compose(**_JUDGE_KW))
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_a_script_that_calls_cap_defines_it():
    script = _compose()
    assert "_cap(" in script
    assert "function _cap(" in script
