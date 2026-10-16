"""A composed ask script dispatches the halted-record commit on a script-side halt, stop_after included."""

from __future__ import annotations

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GATE = "if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true)) {"
_PARAMS = f"run_token: _RUN_TOKEN, {HALTED_RECORD_PARAM}: _halted"
_FN = "async function _commitHaltRecord()"


def _compose(monkeypatch, **over) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_full_path_halt_commit_follows_the_product_commit_stage(monkeypatch):
    script = _compose(monkeypatch, in_run_commit=True)
    assert script.count(_GATE) == 1
    assert script.index("_commit = await agent(") < script.index(_GATE)
    fn = script[script.index(_FN) :].split("\n", 1)[0]
    assert _PARAMS in fn and "script_path: 'scratch/warp/x.mjs'" in fn
    assert "_commitHaltRecord()" in script[script.index(_GATE) :]
    assert "halt_commit: _haltCommit" in script
    assert script.count("let _haltCommit = null;") == 1
    assert script.count("function _terminalCommitCli(") == 1
    assert script.index(_GATE) < script.index("\n  return { arm:")
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_stop_after_halt_commit_precedes_its_single_return(monkeypatch):
    script = _compose(monkeypatch, in_run_commit=True, stop_after=STOP_AFTER_PLAN)
    assert script.count(_GATE) == 1
    assert _PARAMS in script[script.index(_FN) :].split("\n", 1)[0]
    ret = script.index("\n  return _halted ?")
    assert script.index(_GATE) < ret
    assert script.count("\n  return _halted ?") == 1
    assert "halt_commit: _haltCommit" in script[ret:]
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_stop_rule_string_halt_is_typed_before_the_next_action_and_halt_commit(monkeypatch):
    # Textual ordering only: the composed JS is not executed here, so the typed value reaching the
    # halt-commit param is unproven end to end.
    script = _compose(monkeypatch, in_run_commit=True)
    norm = "if (_halted && typeof _halted !== 'object') { _halted = { halted: "
    assert script.count(norm) == 1
    assert "'stop_rule'" in script[script.index(norm) :].split("\n", 1)[0]
    assert script.index(norm) < script.index("\n  _nextAction = ")
    assert script.index(norm) < script.index("\n  const _mkRevise")
    assert script.index(norm) < script.index(_GATE)


def test_without_in_run_commit_no_halt_commit_is_composed(monkeypatch):
    for over in ({}, {"stop_after": STOP_AFTER_PLAN}):
        script = _compose(monkeypatch, **over)
        assert _GATE not in script
        assert _PARAMS not in script and _FN not in script
