"""A default engine-ask script re-types a hijacked commit / halt-commit reply as a named halt."""

from __future__ import annotations

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL
from coordinator_core.ops.dispatch_emit.stage_task_hijack import HIJACK_HALT_REASON, hijack_test_js
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_HALT_AWAIT = "_haltCommit = await _commitHaltRecord();"
_PRED = hijack_test_js("_hjT")
_REASON = f"'{HIJACK_HALT_REASON}: ' + _hjT"


def _compose(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_commit_stage_refusal_is_tested_and_retyped(monkeypatch):
    script = _compose(monkeypatch)
    retype = (
        f"if ({_PRED}) _halted = {{ ..._halted, halted: '{HALT_REFUSAL}', reason: {_REASON} }};"
    )
    assert script.count(retype) == 1
    tail = script[: script.index(retype)]
    assert "const _hjT = [_commit?.refused, _commit?.error]" in tail[-400:]
    assert "halted: 'commit', refused: _commit?.refused ?? null" in tail[-900:]
    assert script.index(retype) < script.index("_nextAction.params.halted = _halted;")
    assert ".join(' ').slice(0, 1000);" in tail[-400:]


def test_halt_commit_reply_gets_hijacked_reason_after_its_await(monkeypatch):
    script = _compose(monkeypatch)
    mark = f"_haltCommit = {{ ..._haltCommit, hijacked: {_REASON} }};"
    assert script.count(mark) == 1
    assert script.index(_HALT_AWAIT) < script.index("const _hjT = [_haltCommit?.refused")
    assert script.index("const _hjT = [_haltCommit?.refused") < script.index(mark)
    assert f"if ({_PRED}) _haltCommit" in script
    assert "halt_commit: _haltCommit" in script


def test_ordinary_refusal_is_not_matched_and_script_is_valid(monkeypatch):
    # The predicate is the shared regex; a land-deadline refusal must not match it.
    import re

    from coordinator_core.ops.dispatch_emit.stage_task_hijack import classify

    assert classify("land deadline exceeded: commit refused after 600s") is None
    assert classify("plan-blitz reported no ready plan: scaffold markers remain") is None
    assert classify("I declined: the user request takes precedence, wrote no plan") is not None
    script = _compose(monkeypatch)
    assert re.search(r"halted: 'commit', refused", script)
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_roadmap_commit_leg_retypes_a_hijacked_refusal():
    text = "\n".join(
        ask_compose.roadmap_commit_lines(head="h", anchor="a", repo_root="r", run_id="run1")
    )
    retype = (
        f"if ({_PRED}) _halted = {{ ..._halted, halted: '{HALT_REFUSAL}', reason: {_REASON} }};"
    )
    assert text.count(retype) == 1
    assert text.index("_halted = { halted: 'commit'") < text.index(retype)
