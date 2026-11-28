"""A leg's ask is the run's recorded ask files; the newest PM turn never stands in for it."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.tests.conftest import unshare_agent_head
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_halt_commits_record import _FN, _compose

_RECORDED = ("pm-intent.md", "relayed-ask.md", "requirements.md")


def test_the_precedence_clause_names_the_recorded_ask_and_refuses_the_newest_turn():
    clause = emit._BRIEF_PRECEDENCE_CLAUSE
    for name in _RECORDED:
        assert f"state/warp/<run>/{name}" in clause
    assert "never substitute the newest PM turn" in clause
    assert "When none of those files is found, say so" in clause


def test_the_halt_commit_leg_prompt_carries_the_recorded_ask_clause(monkeypatch):
    script = unshare_agent_head(_compose(monkeypatch, in_run_commit=True))
    fn = script[script.index(_FN):].split("\n", 1)[0]
    assert ask_compose._PRECEDENCE_TAIL_FN in fn
    tail = script[script.index(f"const {ask_compose._PRECEDENCE_TAIL_FN} = "):].split("\n", 1)[0]
    for name in _RECORDED:
        assert name in tail
    assert "never substitute the newest PM turn" in tail
