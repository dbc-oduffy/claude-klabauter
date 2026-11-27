"""The composed ledger-replan leg branches on the resume ledger's pivot field and halts carry the pivot marker."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import (
    _LEDGER_REPLAN_ADD_ONLY_CLAUSE,
    _LEDGER_REPLAN_PIVOT_CLAUSE,
    _REPLAN_STAGED_SCHEMA,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL


@pytest.fixture
def script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


_ATOM = r"'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`|[A-Za-z_$][\w$]*"
_CHAIN = rf"(?:{_ATOM})(?:\s*\+\s*(?:{_ATOM}))*"
_ATOM_RE = re.compile(rf"({_ATOM})")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _evaluate(script: str, chain: str) -> str:
    """The string a `+`-chain of quoted literals, template literals and hoisted constants evaluates to, however the composer spelled it."""
    parts = []
    for atom in _ATOM_RE.findall(chain):
        if atom[0] in "'`":
            parts.append(re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), atom[1:-1]))
        else:
            decl = re.search(rf"^const {re.escape(atom)} = (.*);$", script, re.MULTILINE)
            assert decl, f"{atom} is not declared in the composed script"
            parts.append(_evaluate(script, decl.group(1)))
    return "".join(parts)


def test_the_replan_prompt_selects_between_the_add_only_and_the_pivot_clause_on_the_pivot_field(script: str) -> None:
    ternaries = re.findall(
        rf"\((?:_gate\.resume_ledger|_lateLedger)\?\.pivot === true \? ({_CHAIN}) : ({_CHAIN})\)", script
    )
    selections = [(_evaluate(script, t), _evaluate(script, f)) for t, f in ternaries]
    assert (_LEDGER_REPLAN_PIVOT_CLAUSE, _LEDGER_REPLAN_ADD_ONLY_CLAUSE) in selections, (
        "the replan prompt does not select a clause on the resume ledger's pivot field"
    )
    assert all(t == _LEDGER_REPLAN_PIVOT_CLAUSE for t, f in selections if f == _LEDGER_REPLAN_ADD_ONLY_CLAUSE)
    assert all(f != _LEDGER_REPLAN_PIVOT_CLAUSE for _, f in selections)


def test_the_pivot_clause_never_touches_landed_rows_and_returns_retired_rows() -> None:
    assert "retired_rows" in _LEDGER_REPLAN_PIVOT_CLAUSE
    assert "retired_rows" in _REPLAN_STAGED_SCHEMA["properties"]
    assert "retired_rows" not in _REPLAN_STAGED_SCHEMA["required"]


def test_the_stage_params_carry_pivot_resume_only_on_a_pivot_resume(script: str) -> None:
    assert "...(_gate.resume_ledger?.pivot === true ? { pivot_resume: true } : {})" in script


def test_a_pivot_resume_owes_the_replan_and_does_not_pre_execute_the_superseded_rows(script: str) -> None:
    assert "const _pivotResume = _resumeLedger && _gate.resume_ledger.pivot === true;" in script
    assert "const _ledgerOwes = _pivotResume ||" in script
    assert "if (!_pivotResume && (_ledgerUnlanded.length || _ledgerDerived.length)) { _ledgerExec = true; }" in script


def test_pull_halts_and_the_ledger_check_halt_carry_the_pivot_marker(script: str) -> None:
    marker = ask_compose._PIVOT_HALT_SPREAD_JS
    assert "pivot: true" in marker and "'PIVOT'" in marker and "'premise-check'" in marker
    assert script.count(marker) == 4
