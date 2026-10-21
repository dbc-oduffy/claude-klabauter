"""The ask script's commit and halt-commit prompts carry the terminal-commit reconcile clause once."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.emit import _INDETERMINATE_RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME, RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_invoke_prompts_carry_reconcile_clause import _prompts

_TERMINAL = f"coordinator-invoke {OP_NAME} --params-file"


def _commit_prompts(script: str) -> list[str]:
    return [p for p in _prompts(script) if _TERMINAL in p]


def _assert_terminal_clause_once(script: str) -> None:
    prompts = _commit_prompts(script)
    assert len(prompts) >= 2, "expected the commit stage and the halt commit"
    for prompt in prompts:
        assert prompt.count(RECONCILE_CLAUSE) == 1, prompt[:200]
        assert _INDETERMINATE_RECONCILE_CLAUSE not in prompt


def test_raw_prompt_arm():
    _assert_terminal_clause_once(_compose(in_run_commit=True))


def test_sizing_arm(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    _assert_terminal_clause_once(_compose(prompt=None, sizing_rel="state/sizings/x.yaml", in_run_commit=True))


def test_commit_schema_admits_already_landed():
    assert ask_compose._COMMIT_SCHEMA["properties"]["already_landed"]["type"] == ["boolean", "null"]


def test_commit_stage_returns_already_landed_and_the_halt_records_it():
    script = _compose(in_run_commit=True)
    for prompt in _commit_prompts(script):
        assert "already_landed" in prompt
    assert "already_landed: _commit?.already_landed ?? null" in script
