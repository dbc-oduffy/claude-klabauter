"""The leg brief says narrowing narrows commit and measure, never the tree."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_NARROWING = (
    "Narrowing a run to its own hunks narrows what the run commits and measures, never the working tree; "
    "never remove or revert a hunk this run did not author."
)


def _script(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_both_ledger_clauses_carry_the_narrowing_sentence() -> None:
    assert _NARROWING in ask_compose._LEDGER_CLAUSE
    assert _NARROWING in ask_compose._RELAYED_LEDGER_CLAUSE
    assert ask_compose._LEDGER_CLAUSE.endswith(_NARROWING)


def test_the_composed_script_carries_the_narrowing_and_own_delta_missing_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    assert "never the working tree" in script
    assert "On an `own_delta_missing:` line, return `zero_run_cause` `own_delta_missing`" in script.replace("\\`", "`")
    assert "never call it a peer's work" in script.replace("\\\\'", "'").replace("\\'", "'")
