"""Both commit prompts name engine_residue and carry RESIDUE_DIRECTIVE; the commit schema admits it."""

from __future__ import annotations

from coordinator_core.git.residue_classes import REPLY_KEY, RESIDUE_DIRECTIVE
from coordinator_core.ops.dispatch_emit.ask_compose import _COMMIT_SCHEMA
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_commit_doc_paths import _FN, _compose


def test_commit_stage_and_halt_prompts_carry_key_and_directive(monkeypatch):
    script = _compose(monkeypatch, in_run_commit=True)
    stage = script[script.index("_terminalCommitParamsBlock({ ..._nextAction.params") :]
    halt = script[script.index(_FN) :]
    for text in (stage, halt):
        assert REPLY_KEY in text
        assert RESIDUE_DIRECTIVE in text


def test_commit_schema_admits_engine_residue_as_nullable_string_array():
    prop = _COMMIT_SCHEMA["properties"][REPLY_KEY]
    assert prop == {"type": ["array", "null"], "items": {"type": "string"}}


def test_script_without_an_in_run_commit_names_no_directive(monkeypatch):
    assert RESIDUE_DIRECTIVE not in _compose(monkeypatch)
