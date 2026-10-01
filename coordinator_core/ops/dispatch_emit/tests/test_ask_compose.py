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


def test_each_halt_returns_before_execute():
    script = _compose()
    halt_at = script.index("_gate.halt || !_gate.arm")
    assert halt_at < script.index("phase('stage')") < script.index("phase('execute')")
    assert script.index("return { halted:", halt_at) < script.index("phase('stage')")
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
